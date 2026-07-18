"""FastAPI REST front-end for the MXW01 printer.

Endpoints:
  GET  /                 web UI for PDF upload
  GET  /health
  GET  /status
  POST /print/text
  POST /print/image
  POST /print/qrcode
  POST /print/pdf
  POST /render/pdf              render to preview without printing
  GET  /render/{id}/page/{n}    preview image (stage selectable)
  POST /render/{id}/print       print a previewed render
  POST /queue          (enqueue a job, same body shapes)
  GET  /queue
  DELETE /queue/{id}
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pipeline import PipelineResult
from preprocess_pdf import NotebookPDFPipeline
from printer import MXW01
from render_config import PipelineConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")

PRINTER_ADDR = os.environ.get("MXW01_ADDRESS", "48:0F:57:49:DB:3B")
INTENSITY = int(os.environ.get("MXW01_INTENSITY", "0x5D"), 0)
RENDER_CONFIG = os.environ.get("RENDER_CONFIG", "rendering.yaml")
render_config = PipelineConfig.from_yaml(RENDER_CONFIG)


class JobKind(str, Enum):
    text = "text"
    image = "image"
    qrcode = "qrcode"
    pdf = "pdf"


class JobState(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


@dataclass
class Job:
    id: str
    kind: JobKind
    payload: dict[str, Any]
    state: JobState = JobState.queued
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None
    retries: int = 0

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "payload": {
                k: (
                    f"<{len(v)} bytes>"
                    if k in {"image_bytes", "pdf_bytes"}
                    else v
                )
                for k, v in self.payload.items()
            },
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "retries": self.retries,
        }


class PrintQueue:
    def __init__(self, printer: MXW01, max_retries: int = 2):
        self.printer = printer
        self.max_retries = max_retries
        self._jobs: dict[str, Job] = {}
        self._order: asyncio.Queue[str] = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()  # printer lock is inside MXW01; this guards job map

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="print-worker")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    async def enqueue(self, kind: JobKind, payload: dict) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, payload=payload)
        async with self._lock:
            self._jobs[job.id] = job
        await self._order.put(job.id)
        log.info("enqueued %s %s", job.id, kind.value)
        return job

    async def list_jobs(self) -> list[dict]:
        async with self._lock:
            return [j.to_dict() for j in self._jobs.values()]

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> Job:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state in (JobState.done, JobState.running):
                raise ValueError(f"cannot cancel job in state {job.state}")
            job.state = JobState.cancelled
            job.touch()
            return job

    async def _run(self) -> None:
        log.info("print worker started")
        while True:
            job_id = await self._order.get()
            async with self._lock:
                job = self._jobs.get(job_id)
            if job is None or job.state == JobState.cancelled:
                continue
            job.state = JobState.running
            job.touch()
            try:
                await self._execute(job)
                job.state = JobState.done
                job.error = None
            except Exception as e:
                job.retries += 1
                job.error = str(e)
                log.exception("job %s failed (try %d)", job.id, job.retries)
                if job.retries <= self.max_retries:
                    job.state = JobState.queued
                    job.touch()
                    await asyncio.sleep(2)
                    await self._order.put(job.id)
                else:
                    job.state = JobState.failed
            job.touch()

    async def _execute(self, job: Job) -> None:
        # Ensure connection; MXW01.connect is idempotent and locked.
        if not self.printer.is_connected:
            await self.printer.connect()
        p = job.payload
        if job.kind == JobKind.text:
            await self.printer.print_text(
                p["text"],
                font_size=p.get("font_size", 32),
                align=p.get("align", "left"),
                intensity=p.get("intensity"),
            )
        elif job.kind == JobKind.qrcode:
            await self.printer.print_qrcode(p["data"], intensity=p.get("intensity"))
        elif job.kind == JobKind.image:
            await self.printer.print_image(
                p["image_bytes"], intensity=p.get("intensity")
            )
        elif job.kind == JobKind.pdf:
            render_id = p.get("render_id")
            if render_id and render_id in RENDERS:
                # Print an already-previewed render without re-processing.
                pages = RENDERS[render_id].pages
            else:
                result = await asyncio.to_thread(
                    pipeline.process,
                    p["pdf_bytes"],
                    p.get("filename", "upload.pdf"),
                )
                RENDERS[result.output_dir.name] = result
                p["render_id"] = result.output_dir.name
                p["report_path"] = str(result.report_path)
                pages = result.pages
            for page in pages:
                await self.printer.print_packed(
                    page.bitmap.packed,
                    width=page.bitmap.width,
                    height=page.bitmap.height,
                    intensity=p.get("intensity"),
                )
        else:
            raise ValueError(f"unknown kind {job.kind}")


# ----- request models -----------------------------------------------------

class TextBody(BaseModel):
    text: str
    font_size: int = 32
    align: str = Field(default="left", pattern="^(left|center|right)$")
    intensity: Optional[int] = None
    queue: bool = False


class QRBody(BaseModel):
    data: str
    intensity: Optional[int] = None
    queue: bool = False


pipeline = NotebookPDFPipeline(
    output_dir=render_config.output_dir,
    printer_width=render_config.printer_width,
    minimum_rows=render_config.minimum_page_rows,
)
# Rendered-but-not-yet-printed results, keyed by render id (output dir name).
# Lets the UI preview pages and then print the exact same bitmaps.
RENDERS: dict[str, PipelineResult] = {}
RENDER_SOURCES: dict[str, dict[str, Any]] = {}
printer = MXW01(
    PRINTER_ADDR,
    intensity=INTENSITY,
    width=render_config.printer_width,
    minimum_rows=render_config.minimum_page_rows,
)
queue = PrintQueue(printer)


async def _connect_in_background() -> None:
    try:
        await printer.connect()
        log.info("initial printer connect done")
    except Exception as e:
        log.warning("initial connect failed (will retry on demand): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    queue.start()
    # Don't block port binding on the (slow) BLE connect — do it in background.
    connect_task = asyncio.create_task(_connect_in_background())
    yield
    connect_task.cancel()
    await queue.stop()
    await printer.disconnect()


app = FastAPI(title="MXW01 Print Server", version="1.0.0", lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def ui():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "printer": PRINTER_ADDR,
        "connected": printer.is_connected,
    }


@app.get("/status")
async def status():
    try:
        if not printer.is_connected:
            await printer.connect()
        st = await printer.status()
        return st.to_dict()
    except Exception as e:
        raise HTTPException(503, f"printer unavailable: {e}") from e


@app.post("/print/text")
async def print_text(body: TextBody):
    payload = body.model_dump()
    payload.pop("queue", None)
    if body.queue:
        job = await queue.enqueue(JobKind.text, payload)
        return {"queued": True, "job": job.to_dict()}
    try:
        if not printer.is_connected:
            await printer.connect()
        await printer.print_text(
            body.text,
            font_size=body.font_size,
            align=body.align,
            intensity=body.intensity,
        )
        return {"queued": False, "ok": True}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@app.post("/print/qrcode")
async def print_qrcode(body: QRBody):
    payload = {"data": body.data, "intensity": body.intensity}
    if body.queue:
        job = await queue.enqueue(JobKind.qrcode, payload)
        return {"queued": True, "job": job.to_dict()}
    try:
        if not printer.is_connected:
            await printer.connect()
        await printer.print_qrcode(body.data, intensity=body.intensity)
        return {"queued": False, "ok": True}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@app.post("/print/image")
async def print_image(
    file: UploadFile = File(...),
    intensity: Optional[int] = Form(default=None),
    queue_job: bool = Form(default=False, alias="queue"),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    payload = {"image_bytes": data, "intensity": intensity, "filename": file.filename}
    if queue_job:
        job = await queue.enqueue(JobKind.image, payload)
        return {"queued": True, "job": job.to_dict()}
    try:
        if not printer.is_connected:
            await printer.connect()
        await printer.print_image(data, intensity=intensity)
        return {"queued": False, "ok": True}
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@app.post("/print/pdf")
async def print_pdf(
    file: UploadFile = File(...),
    intensity: Optional[int] = Form(default=None),
    queue_job: bool = Form(default=True, alias="queue"),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "uploaded file is not a PDF")
    payload = {
        "pdf_bytes": data,
        "intensity": intensity,
        "filename": file.filename or "upload.pdf",
    }
    if queue_job:
        job = await queue.enqueue(JobKind.pdf, payload)
        return {"queued": True, "job": job.to_dict()}
    try:
        result = await asyncio.to_thread(
            pipeline.process, data, file.filename or "upload.pdf"
        )
        if not printer.is_connected:
            await printer.connect()
        for page in result.pages:
            await printer.print_packed(
                page.bitmap.packed,
                width=page.bitmap.width,
                height=page.bitmap.height,
                intensity=intensity,
            )
        return {
            "queued": False,
            "ok": True,
            "pages": len(result.pages),
            "report_path": str(result.report_path),
            "report": [page.report for page in result.pages],
        }
    except Exception as e:
        raise HTTPException(500, str(e)) from e


def _render_summary(render_id: str, result: PipelineResult) -> dict:
    return {
        "render_id": render_id,
        "pages": [
            {
                "index": page.page_number,
                "number": page.page_number + 1,
                "width": page.bitmap.width,
                "height": page.bitmap.height,
                "orientation": page.report.get("chosen_orientation"),
                "threshold": page.report.get("chosen_threshold_algorithm"),
                "dither": page.report.get("chosen_dithering"),
                "page_type": page.report.get("page_type"),
                "preview_url": f"/render/{render_id}/page/{page.page_number + 1}",
                "stages_url": f"/render/{render_id}/page/{page.page_number + 1}?stage=03_cropped",
            }
            for page in result.pages
        ],
        "report_path": str(result.report_path),
    }


@app.post("/render/pdf")
async def render_pdf(
    file: UploadFile = File(...),
    intensity: Optional[int] = Form(default=None),
):
    """Render a PDF to previewable bitmaps WITHOUT printing.

    Returns a render_id and per-page preview URLs. Call POST /render/{id}/print
    to print the exact bitmaps that were previewed.
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "uploaded file is not a PDF")
    try:
        result = await asyncio.to_thread(
            pipeline.process, data, file.filename or "upload.pdf"
        )
    except Exception as e:
        raise HTTPException(500, f"render failed: {e}") from e
    render_id = result.output_dir.name
    RENDERS[render_id] = result
    RENDER_SOURCES[render_id] = {
        "pdf_bytes": data,
        "filename": file.filename or "upload.pdf",
        "intensity": intensity,
    }
    return _render_summary(render_id, result)


@app.get("/render/{render_id}/page/{page_number}")
async def render_preview(render_id: str, page_number: int, stage: str = "09_final"):
    result = RENDERS.get(render_id)
    if result is None:
        raise HTTPException(404, "render not found (may have expired)")
    if not (1 <= page_number <= len(result.pages)):
        raise HTTPException(404, "page out of range")
    page = result.pages[page_number - 1]
    allowed = {
        "01_original", "02_rendered", "03_cropped", "04_rotated", "05_scaled",
        "06_enhanced", "07_threshold", "08_dithered", "09_final",
    }
    if stage not in allowed:
        raise HTTPException(400, f"stage must be one of {sorted(allowed)}")
    image_path = page.output_dir / f"{stage}.png"
    if not image_path.exists():
        raise HTTPException(404, "preview image not found")
    return FileResponse(image_path, media_type="image/png")


@app.post("/render/{render_id}/print")
async def render_print(render_id: str, intensity: Optional[int] = Form(default=None)):
    if render_id not in RENDERS:
        raise HTTPException(404, "render not found (may have expired)")
    source = RENDER_SOURCES.get(render_id, {})
    payload = {
        "render_id": render_id,
        "pdf_bytes": source.get("pdf_bytes", b""),
        "filename": source.get("filename", "upload.pdf"),
        "intensity": intensity if intensity is not None else source.get("intensity"),
    }
    job = await queue.enqueue(JobKind.pdf, payload)
    return {"queued": True, "job": job.to_dict()}


@app.post("/queue")
async def queue_add(body: dict[str, Any]):
    """Generic enqueue. Body must include `kind` in {text,image,qrcode} plus
    the fields expected by that kind. For image, pass base64 in `image_b64`."""
    kind_s = body.get("kind")
    try:
        kind = JobKind(kind_s)
    except Exception as e:
        raise HTTPException(400, "kind must be text|image|qrcode|pdf") from e
    payload = dict(body)
    payload.pop("kind", None)
    if kind == JobKind.image:
        import base64

        if "image_b64" in payload:
            payload["image_bytes"] = base64.b64decode(payload.pop("image_b64"))
        elif "image_bytes" not in payload:
            raise HTTPException(400, "image job needs image_b64")
    elif kind == JobKind.pdf:
        import base64

        if "pdf_b64" in payload:
            payload["pdf_bytes"] = base64.b64decode(payload.pop("pdf_b64"))
        elif "pdf_bytes" not in payload:
            raise HTTPException(400, "pdf job needs pdf_b64")
    job = await queue.enqueue(kind, payload)
    return job.to_dict()


@app.get("/queue")
async def queue_list():
    return await queue.list_jobs()


@app.delete("/queue/{job_id}")
async def queue_delete(job_id: str):
    try:
        job = await queue.cancel(job_id)
        return job.to_dict()
    except KeyError:
        raise HTTPException(404, "job not found") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.exception_handler(Exception)
async def _unhandled(_req, exc):
    log.exception("unhandled: %s", exc)
    return JSONResponse({"detail": str(exc)}, status_code=500)
