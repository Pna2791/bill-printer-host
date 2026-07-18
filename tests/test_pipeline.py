from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import fitz
import numpy as np
from PIL import Image

from pipeline import PDFPipeline
from render_config import PipelineConfig
from renderer import pack_1bpp


def make_test_pdf() -> bytes:
    document = fitz.open()
    # Landscape vector/text receipt with intentionally large white margins.
    page = document.new_page(width=600, height=300)
    page.insert_text((130, 105), "SHARP VECTOR RECEIPT", fontsize=24)
    page.insert_text((130, 140), "Item A     12.00", fontsize=16)
    page.draw_rect(fitz.Rect(120, 75, 480, 165), color=(0, 0, 0), width=1)

    # Portrait mixed page: vector text plus a grayscale raster.
    page = document.new_page(width=300, height=600)
    page.insert_text((55, 80), "PORTRAIT DOCUMENT", fontsize=18)
    gradient = np.tile(np.linspace(0, 255, 180, dtype=np.uint8), (100, 1))
    image = Image.fromarray(gradient, mode="L")
    with tempfile.NamedTemporaryFile(suffix=".png") as image_file:
        image.save(image_file.name)
        page.insert_image(fitz.Rect(60, 130, 240, 230), filename=image_file.name)
    page.insert_text((55, 280), "Text under image", fontsize=15)
    data = document.tobytes()
    document.close()
    return data


class PipelineTests(unittest.TestCase):
    def test_multipage_render_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = PipelineConfig(
                printer_width=384,
                render_dpi=300,
                cache_dir=str(Path(directory) / "cache"),
                output_dir=str(Path(directory) / "output"),
                save_intermediates=True,
            )
            result = PDFPipeline(config).process(make_test_pdf(), job_name="quality")
            self.assertEqual(len(result.pages), 2)
            report = json.loads(result.report_path.read_text())
            self.assertEqual(report["page_count"], 2)
            for page in result.pages:
                self.assertEqual(page.bitmap.width, config.printer_width)
                self.assertEqual(
                    len(page.bitmap.packed),
                    page.bitmap.height * config.printer_width // 8,
                )
                self.assertIn(
                    page.report["chosen_threshold_algorithm"],
                    config.threshold_candidates,
                )
                self.assertIn(
                    page.report["chosen_dithering"], config.dither_candidates
                )
                for number, name in enumerate(
                    [
                        "original",
                        "rendered",
                        "cropped",
                        "rotated",
                        "scaled",
                        "enhanced",
                        "threshold",
                        "dithered",
                        "final",
                    ],
                    start=1,
                ):
                    self.assertTrue(
                        (page.output_dir / f"{number:02d}_{name}.png").exists()
                    )
            self.assertIn(result.pages[0].report["chosen_orientation"], [90, 270])
            self.assertIn(result.pages[1].report["chosen_orientation"], [0, 180])

    def test_lsb_left_bit_packing(self) -> None:
        pixels = np.full((1, 8), 255, dtype=np.uint8)
        pixels[0, 0] = 0
        pixels[0, 7] = 0
        result = pack_1bpp(Image.fromarray(pixels), width=8)
        self.assertEqual(result.packed, b"\x81")

    def test_non_byte_aligned_width_rejected(self) -> None:
        config = PipelineConfig(printer_width=385)
        with self.assertRaises(ValueError):
            config.validate()


if __name__ == "__main__":
    unittest.main()
