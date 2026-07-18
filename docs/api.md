# REST API

Server: `uvicorn api:app --host 0.0.0.0 --port 8080`
Environment:

| Variable | Default | Meaning |
|---|---|---|
| `MXW01_ADDRESS` | `48:0F:57:49:DB:3B` | Printer BLE MAC |
| `MXW01_INTENSITY` | `0x5D` | Default print energy |
| `RENDER_CONFIG` | `rendering.yaml` | PDF rendering YAML |

## Endpoints

### `GET /`
Browser UI for PDF upload, printer status, and queue monitoring.
Static assets are served from `/static/`.

### `GET /health`
Liveness. Does not talk to the printer beyond reporting cached connection state.

```json
{"ok": true, "printer": "48:0F:57:49:DB:3B", "connected": true}
```

### `GET /status`
Queries A1 (and B1 once) over BLE. Returns battery, temperature, firmware.

### `POST /print/text`
```json
{"text": "Hello", "font_size": 32, "align": "left", "intensity": 93, "queue": false}
```
If `queue` is true the job is enqueued and the response contains the job id.

### `POST /print/qrcode`
```json
{"data": "https://example.com", "queue": false}
```

### `POST /print/image`
`multipart/form-data` with fields:
- `file` — image bytes (PNG/JPEG/…)
- `intensity` — optional int
- `queue` — optional bool

### `POST /print/pdf`
`multipart/form-data` with fields:
- `file` — PDF file
- `intensity` — optional print intensity
- `queue` — defaults to `true`; PDF rendering and multipage printing run in
  the background queue

Example:

```bash
curl -X POST http://localhost:8080/print/pdf \
  -F file=@receipt.pdf -F queue=true
```

Each page is rendered independently. Preview stages, `bitmap.bin`, and quality
reports are saved under `render_output/`. For synchronous requests (`queue=false`)
the response includes every page report and the document report path.

### `POST /render/pdf`
Render a PDF to previewable bitmaps **without printing**. Same form fields as
`/print/pdf` (minus `queue`). Returns a `render_id` and per-page metadata with
preview URLs:

```bash
curl -X POST http://localhost:8080/render/pdf -F file=@receipt.pdf
```

```json
{
  "render_id": "receipt_pdf_23f5a53737ae",
  "pages": [
    {
      "number": 1, "width": 384, "height": 550,
      "threshold": "otsu", "dither": "floyd_steinberg",
      "orientation": 0, "page_type": "photo",
      "preview_url": "/render/receipt_pdf_23f5a53737ae/page/1"
    }
  ]
}
```

### `GET /render/{render_id}/page/{n}`
Returns the preview PNG for page `n` (1-based). Optional `stage` query selects
an intermediate: `01_original`, `02_rendered`, `03_cropped`, `04_rotated`,
`05_scaled`, `06_enhanced`, `07_threshold`, `08_dithered`, `09_final` (default —
exactly what will be printed).

### `POST /render/{render_id}/print`
Queue the previously previewed render for printing. The exact previewed bitmaps
are reused — no re-rendering. Optional `intensity` form field overrides the one
given at render time. Renders are held in memory; after a server restart the
render must be created again.

The web UI at `/` uses this flow: upload → render preview → confirm print.

### `POST /queue`
Generic enqueue. Body must include `kind` ∈ `text|image|qrcode|pdf` plus the
payload fields for that kind. For images use `image_b64`; for PDFs use
`pdf_b64`.

### `GET /queue`
List all jobs (queued / running / done / failed / cancelled).

### `DELETE /queue/{id}`
Cancel a queued job. Running/done jobs return HTTP 409.

## Behaviour

- Background worker drains the queue FIFO.
- Failed jobs retry up to 2 times with a 2 s backoff.
- `MXW01` serialises all BLE traffic with an asyncio lock.
- On startup the server tries to connect; failure is non-fatal — the next
  request reconnects.
