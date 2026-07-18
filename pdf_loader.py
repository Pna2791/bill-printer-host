"""High-DPI PDF rendering with metadata extraction and disk caching."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image


@dataclass
class PDFPage:
    number: int
    image: Image.Image
    width_points: float
    height_points: float
    text_blocks: list[dict]
    image_blocks: list[dict]
    source_rotation: int


class PDFLoader:
    def __init__(self, render_dpi: int, cache_dir: str | Path):
        self.render_dpi = render_dpi
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _document_key(self, pdf_bytes: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(pdf_bytes)
        digest.update(f":dpi={self.render_dpi}".encode())
        return digest.hexdigest()

    def render_clip(
        self,
        pdf_bytes: bytes,
        page_number: int,
        rect_points: tuple[float, float, float, float],
        dpi: int,
    ) -> Image.Image:
        """Re-rasterize a page region at a higher DPI (grayscale, on white).

        Used for machine-readable symbols that could not be decoded: they are
        re-rendered at up to 1200 DPI so thresholding sees exact geometry.
        """
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        try:
            page = document[page_number]
            scale = dpi / 72.0
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                clip=fitz.Rect(*rect_points),
                colorspace=fitz.csGRAY,
                alpha=False,
                annots=True,
            )
            return Image.frombytes(
                "L", (pixmap.width, pixmap.height), pixmap.samples
            )
        finally:
            document.close()

    def load(self, source: str | Path | bytes) -> list[PDFPage]:
        if isinstance(source, bytes):
            pdf_bytes = source
        else:
            pdf_bytes = Path(source).read_bytes()
        key = self._document_key(pdf_bytes)
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        pages: list[PDFPage] = []
        try:
            for number, page in enumerate(document):
                pages.append(self._load_page(page, number, key))
        finally:
            document.close()
        return pages

    def _load_page(self, page: fitz.Page, number: int, key: str) -> PDFPage:
        page_dir = self.cache_dir / key
        page_dir.mkdir(parents=True, exist_ok=True)
        image_path = page_dir / f"page_{number:04d}.png"
        metadata_path = page_dir / f"page_{number:04d}.json"

        if image_path.exists() and metadata_path.exists():
            image = Image.open(image_path).convert("RGB")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        else:
            scale = self.render_dpi / 72.0
            pixmap = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                colorspace=fitz.csRGB,
                alpha=True,
                annots=True,
            )
            image = Image.frombytes("RGBA", (pixmap.width, pixmap.height), pixmap.samples)
            # Composite transparency onto white so transparent margins crop correctly.
            white = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(white, image).convert("RGB")
            image.save(image_path, optimize=True)

            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_IMAGES).get(
                "blocks", []
            )
            text_blocks: list[dict] = []
            image_blocks: list[dict] = []
            for block in blocks:
                bbox = [float(v) for v in block.get("bbox", (0, 0, 0, 0))]
                if block.get("type") == 0:
                    chars = sum(
                        len(span.get("text", ""))
                        for line in block.get("lines", [])
                        for span in line.get("spans", [])
                    )
                    directions = [
                        list(line.get("dir", (1.0, 0.0)))
                        for line in block.get("lines", [])
                    ]
                    text_blocks.append(
                        {"bbox": bbox, "characters": chars, "directions": directions}
                    )
                elif block.get("type") == 1:
                    image_blocks.append({"bbox": bbox})
            metadata = {
                "width_points": float(page.rect.width),
                "height_points": float(page.rect.height),
                "source_rotation": int(page.rotation),
                "text_blocks": text_blocks,
                "image_blocks": image_blocks,
            }
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        return PDFPage(
            number=number,
            image=image,
            width_points=metadata["width_points"],
            height_points=metadata["height_points"],
            text_blocks=metadata["text_blocks"],
            image_blocks=metadata["image_blocks"],
            source_rotation=metadata["source_rotation"],
        )
