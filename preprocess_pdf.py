"""PDF -> print-ready image file, using the exact logic from prepocess.ipynb.

Steps (unchanged from the notebook):
  1. Render first page at 600 DPI.
  2. Convert to black & white with a hard threshold at 196 (no dithering).
  3. Crop to the bounding box of the content.
  4. Resize to 384 px wide (printer width) with NEAREST resampling.
  5. Save the result — this file is what gets printed.

Usage:
    python preprocess_pdf.py input.pdf [-o output.png]
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from pdf2image import convert_from_bytes, convert_from_path
from PIL import Image

from pipeline import PageResult, PipelineResult
from renderer import pack_1bpp


def process_pdf_first_page(pdf_source):
    """
    Converts the first page of a PDF to an image, converts to black & white,
    and crops to bounding box of content.
    Returns the original, bw, and b&w-cropped PIL images.
    """
    if isinstance(pdf_source, bytes):
        pil_images = convert_from_bytes(
            pdf_source, first_page=1, last_page=1, dpi=600
        )
    else:
        pil_images = convert_from_path(
            pdf_source, first_page=1, last_page=1, dpi=600
        )
    if not pil_images:
        raise ValueError("No page found in PDF")
    img = pil_images[0]

    bw_image = img.convert("L").point(lambda x: 0 if x < 196 else 255, mode="1")

    def find_content_bounds(bw_img):
        arr = np.array(bw_img)
        rows = np.any(arr == 0, axis=1)
        cols = np.any(arr == 0, axis=0)
        if not np.any(rows) or not np.any(cols):
            return None
        top = np.argmax(rows)
        bot = len(rows) - np.argmax(rows[::-1]) - 1
        left = np.argmax(cols)
        right = len(cols) - np.argmax(cols[::-1]) - 1
        return top, bot, left, right

    bounds = find_content_bounds(bw_image)
    if bounds:
        top, bot, left, right = bounds
        bw_cropped = bw_image.crop((left + 1, top + 1, right + 1, bot + 1))
    else:
        bw_cropped = bw_image

    return img, bw_image, bw_cropped


def resize_to_printer_width(image, target_width=384):
    w, h = image.size
    if w == target_width:
        return image
    new_height = int(h * (target_width / w))
    return image.resize((target_width, new_height), resample=Image.NEAREST)


def rotate_landscape(image):
    """Rotate landscape content 90 degrees clockwise before resizing."""
    if image.height < image.width:
        return image.transpose(Image.Transpose.ROTATE_270)
    return image


class NotebookPDFPipeline:
    """Web-compatible adapter for the preprocessing logic in prepocess.ipynb."""

    def __init__(
        self,
        output_dir="render_output",
        printer_width=384,
        minimum_rows=1,
    ):
        self.output_dir = Path(output_dir)
        self.printer_width = printer_width
        self.minimum_rows = minimum_rows

    def process(self, source, job_name=None):
        pdf_bytes = source if isinstance(source, bytes) else Path(source).read_bytes()
        job_key = hashlib.sha256(pdf_bytes).hexdigest()[:12]
        if job_name:
            safe_name = "".join(
                char if char.isalnum() or char in "-_" else "_"
                for char in job_name
            )
            job_key = f"{safe_name}_{job_key}"

        output_dir = self.output_dir / job_key
        page_dir = output_dir / "page_0001"
        page_dir.mkdir(parents=True, exist_ok=True)

        original, thresholded, cropped = process_pdf_first_page(pdf_bytes)
        rotation = 90 if cropped.height < cropped.width else 0
        rotated = rotate_landscape(cropped)
        final = resize_to_printer_width(rotated, self.printer_width)
        bitmap = pack_1bpp(
            final,
            width=self.printer_width,
            minimum_rows=self.minimum_rows,
        )

        # Keep the stage filenames expected by the existing preview endpoint.
        stages = {
            "01_original.png": original,
            "02_rendered.png": original,
            "03_cropped.png": cropped,
            "04_rotated.png": rotated,
            "05_scaled.png": final,
            "06_enhanced.png": final,
            "07_threshold.png": thresholded,
            "08_dithered.png": final,
            "09_final.png": bitmap.image,
        }
        for filename, image in stages.items():
            image.save(page_dir / filename, optimize=True)
        (page_dir / "bitmap.bin").write_bytes(bitmap.packed)

        report = {
            "page": 1,
            "pipeline": "notebook",
            "source_size_pixels": list(original.size),
            "chosen_orientation": rotation,
            "chosen_threshold_algorithm": "fixed_196",
            "chosen_dithering": "none",
            "final_width": bitmap.width,
            "final_height": bitmap.height,
            "bytes_per_row": bitmap.bytes_per_row,
            "packed_bytes": len(bitmap.packed),
        }
        (page_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )

        page = PageResult(0, bitmap, report, page_dir)
        document_report = {
            "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "page_count": 1,
            "pipeline": "notebook",
            "pages": [report],
        }
        report_path = output_dir / "report.json"
        report_path.write_text(
            json.dumps(document_report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return PipelineResult([page], report_path, output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="input PDF file")
    parser.add_argument("-o", "--output", default="output.png",
                        help="print file to create (default: output.png)")
    args = parser.parse_args()

    _, _, bw_cropped = process_pdf_first_page(args.pdf)
    rotated = rotate_landscape(bw_cropped)
    bw_resized = resize_to_printer_width(rotated)
    bw_resized.save(args.output)
    print(f"{args.output}: {bw_resized.size[0]}x{bw_resized.size[1]}")


if __name__ == "__main__":
    main()
