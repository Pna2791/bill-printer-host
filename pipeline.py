"""End-to-end, high-quality PDF to thermal-printer bitmap pipeline."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

from cropper import crop_binary_vertical, crop_visible_content, remove_isolated_noise
from dither import choose_dither, error_diffusion
from enhancer import enhance
from orientation import choose_orientation
from page_analyzer import analyze_page
from pdf_loader import PDFLoader, PDFPage
from regions import (
    Region,
    RegionSet,
    detect_regions,
    map_bbox_to_final,
    regenerate_barcode,
    regenerate_qr,
    rethreshold_symbol,
)
from render_config import PipelineConfig
from renderer import RenderedBitmap, pack_1bpp, scale_to_width
from threshold import apply_threshold, choose_threshold


@dataclass
class PageResult:
    page_number: int
    bitmap: RenderedBitmap
    report: dict
    output_dir: Path


@dataclass
class PipelineResult:
    pages: list[PageResult]
    report_path: Path
    output_dir: Path


def _side_blank_columns(
    binary: Image.Image, minimum_total: int = 4
) -> tuple[int, int] | None:
    """Blank (ink-free) column counts at the left and right edges.

    Returns None when the sides are already used, so the caller can skip the
    second render pass.
    """
    array = np.asarray(binary.convert("L"), dtype=np.uint8)
    ink_columns = np.flatnonzero(np.any(array < 128, axis=0))
    if len(ink_columns) == 0:
        return None
    blank_left = int(ink_columns[0])
    blank_right = int(array.shape[1] - 1 - ink_columns[-1])
    if blank_left + blank_right < minimum_total:
        return None
    return blank_left, blank_right


class PDFPipeline:
    def __init__(self, config: PipelineConfig):
        config.validate()
        self.config = config
        self.loader = PDFLoader(config.render_dpi, config.cache_dir)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PDFPipeline":
        return cls(PipelineConfig.from_yaml(path))

    def _binarize(self, scaled: Image.Image, page_type: str):
        enhanced = enhance(scaled, page_type, self.config.enhancement)
        thresholded = choose_threshold(
            enhanced.image,
            page_type,
            self.config.threshold_candidates,
            forced=self.config.threshold_algorithm,
        )
        dithered = choose_dither(
            enhanced.image,
            thresholded.image,
            page_type,
            self.config.dither_candidates,
            forced=self.config.dither_algorithm,
        )
        return enhanced, thresholded, dithered

    def _composite_content_regions(
        self,
        final_array: np.ndarray,
        enhanced_image: Image.Image,
        regions: RegionSet,
        mapping: dict,
    ) -> np.ndarray:
        """Overwrite text/logo/photo regions with a dedicated binarization."""
        if not self.config.regions_enabled:
            return final_array
        gray = np.asarray(enhanced_image.convert("L"), dtype=np.uint8)
        height, width = final_array.shape
        page_area = height * width
        result = final_array.copy()
        for region in [*regions.photos, *regions.texts, *regions.logos]:
            fx0, fy0, fx1, fy1 = map_bbox_to_final(
                region.bbox, top_offset=0, **mapping
            )
            fx0, fy0 = max(0, fx0), max(0, fy0)
            fx1, fy1 = min(width, fx1), min(height, fy1)
            if fx1 - fx0 < 8 or fy1 - fy0 < 8:
                continue
            # Full-page regions are already what the base pass optimized for.
            if (fx1 - fx0) * (fy1 - fy0) > 0.85 * page_area:
                continue
            roi = gray[fy0:fy1, fx0:fx1]
            if region.kind == "photo":
                result[fy0:fy1, fx0:fx1] = error_diffusion(roi, "floyd_steinberg")
                region.action = "region-dither"
            elif region.kind == "logo":
                result[fy0:fy1, fx0:fx1] = apply_threshold(roi, "otsu")
                region.action = "region-threshold"
            else:  # text
                chosen = choose_threshold(
                    Image.fromarray(roi, mode="L"),
                    "text",
                    self.config.threshold_candidates,
                    forced=self.config.threshold_algorithm,
                )
                result[fy0:fy1, fx0:fx1] = np.asarray(
                    chosen.image.convert("L"), dtype=np.uint8
                )
                region.action = f"region-threshold:{chosen.algorithm}"
        return result

    def _composite_symbols(
        self,
        final_array: np.ndarray,
        regions: RegionSet,
        mapping: dict,
        top_offset: int,
        page: PDFPage,
        crop_box: tuple[int, int, int, int],
        pdf_bytes: bytes | None,
    ) -> np.ndarray:
        """Paste geometry-exact QR/barcode renderings over the base output."""
        if not self.config.regions_enabled or not regions.symbols:
            return final_array
        height, width = final_array.shape
        result = final_array.copy()
        for region in regions.symbols:
            fx0, fy0, fx1, fy1 = map_bbox_to_final(
                region.bbox, top_offset=top_offset, **mapping
            )
            target_w = fx1 - fx0
            target_h = fy1 - fy0
            if target_w < 16 or target_h < 8:
                continue

            symbol: np.ndarray | None = None
            if region.decoded and region.kind == "qr":
                symbol = regenerate_qr(
                    region.payload,
                    bbox_dots=max(target_w, target_h),
                    page_width_dots=width,
                    quiet_modules=self.config.symbol_quiet_modules,
                    min_module_dots=self.config.symbol_min_module_dots,
                )
                if symbol is not None:
                    region.action = "regenerated"
            elif region.decoded and region.kind == "barcode":
                symbol = regenerate_barcode(
                    region.payload,
                    region.symbol_type,
                    bbox_width_dots=target_w,
                    bbox_height_dots=target_h,
                    page_width_dots=width,
                )
                if symbol is not None:
                    region.action = "regenerated"

            if symbol is None:
                # Undecodable (or unsupported symbology): re-rasterize the
                # source rectangle at high DPI and threshold it untouched.
                source = self._symbol_source_gray(region, page, crop_box, pdf_bytes)
                rotated = np.rot90(source, k=mapping["angle"] // 90)
                symbol = rethreshold_symbol(rotated, target_w, target_h)
                region.action = "rethresholded"

            sh, sw = symbol.shape
            # Center on the original bbox, clamped to the page.
            px0 = max(0, min(width - sw, fx0 + (target_w - sw) // 2))
            py0 = max(0, min(height - sh, fy0 + (target_h - sh) // 2))
            if sw > width or sh > height:
                continue
            # Clear the area first so the quiet zone is genuinely quiet. The
            # margin also covers original bars that bled past the detected
            # bbox through LANCZOS resampling (~2-4 dots).
            clear = 6
            cx0 = max(0, min(px0, fx0) - clear)
            cy0 = max(0, min(py0, fy0) - clear)
            cx1 = min(width, max(px0 + sw, fx1) + clear)
            cy1 = min(height, max(py0 + sh, fy1) + clear)
            result[cy0:cy1, cx0:cx1] = 255
            result[py0 : py0 + sh, px0 : px0 + sw] = symbol
        return result

    def _symbol_source_gray(
        self,
        region: Region,
        page: PDFPage,
        crop_box: tuple[int, int, int, int],
        pdf_bytes: bytes | None,
    ) -> np.ndarray:
        """Unenhanced grayscale for a symbol, re-rendered at high DPI if possible."""
        x0, y0, x1, y1 = region.bbox
        if pdf_bytes is not None:
            pixels_to_points = 72.0 / self.config.render_dpi
            rect = (
                (x0 + crop_box[0]) * pixels_to_points,
                (y0 + crop_box[1]) * pixels_to_points,
                (x1 + crop_box[0]) * pixels_to_points,
                (y1 + crop_box[1]) * pixels_to_points,
            )
            try:
                clip = self.loader.render_clip(
                    pdf_bytes,
                    page.number,
                    rect,
                    dpi=self.config.symbol_fallback_dpi,
                )
                return np.asarray(clip, dtype=np.uint8)
            except Exception:
                pass
        full = np.asarray(page.image.convert("L"), dtype=np.uint8)
        return full[
            y0 + crop_box[1] : y1 + crop_box[1], x0 + crop_box[0] : x1 + crop_box[0]
        ]

    def process(self, source: str | Path | bytes, job_name: str | None = None) -> PipelineResult:
        pdf_bytes = source if isinstance(source, bytes) else Path(source).read_bytes()
        job_key = hashlib.sha256(pdf_bytes).hexdigest()[:12]
        if job_name:
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in job_name)
            job_key = f"{safe_name}_{job_key}"
        output_dir = Path(self.config.output_dir) / job_key
        output_dir.mkdir(parents=True, exist_ok=True)

        pages = self.loader.load(pdf_bytes)
        results = [
            self.process_page(page, output_dir, pdf_bytes=pdf_bytes)
            for page in pages
        ]
        document_report = {
            "source_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "page_count": len(results),
            "configuration": self.config.to_dict(),
            "pages": [result.report for result in results],
        }
        report_path = output_dir / "report.json"
        report_path.write_text(
            json.dumps(document_report, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return PipelineResult(results, report_path, output_dir)

    def process_page(
        self, page: PDFPage, document_dir: Path, pdf_bytes: bytes | None = None
    ) -> PageResult:
        page_dir = document_dir / f"page_{page.number + 1:04d}"
        page_dir.mkdir(parents=True, exist_ok=True)
        stages: dict[str, Image.Image] = {}

        stages["01_original.png"] = page.image
        # Explicitly record the cached high-DPI raster as a separate stage.
        stages["02_rendered.png"] = page.image.copy()

        # Margins are removed immediately after rasterization; every later
        # stage (analysis, orientation, scaling, binarization) sees only content.
        cropped, crop_box = crop_visible_content(
            page.image,
            white_threshold=self.config.white_threshold,
            margin=self.config.crop_margin,
            safety_margin=self.config.content_safety_margin,
        )
        stages["03_cropped.png"] = cropped

        crop_fraction = (cropped.width * cropped.height) / max(
            1, page.image.width * page.image.height
        )
        analysis = analyze_page(
            page,
            cropped,
            white_threshold=self.config.white_threshold,
            metadata_area_fraction=crop_fraction,
        )

        # Region detection runs on the cropped, UNENHANCED raster so symbol
        # geometry is still faithful and enhancement can be bypassed for them.
        regions = RegionSet()
        if self.config.regions_enabled:
            regions = detect_regions(
                cropped,
                page.text_blocks,
                page.image_blocks,
                points_to_pixels=self.config.render_dpi / 72.0,
                crop_offset=(crop_box[0], crop_box[1]),
            )

        orientation = choose_orientation(
            cropped,
            page,
            printer_width=self.config.printer_width,
            mode=self.config.rotation_mode,
        )
        stages["04_rotated.png"] = orientation.image

        oriented = orientation.image
        scaled, scale_factor = scale_to_width(oriented, self.config.printer_width)
        enhanced, thresholded, dithered = self._binarize(scaled, analysis.page_type)

        # Second pass: crop padding, anti-aliased halos and rotation margins can
        # leave blank columns at the sides after binarization. Re-crop the
        # oriented page horizontally and re-render so content spans the full
        # printable width.
        side_blanks = _side_blank_columns(dithered.image)
        side_trim = [0, 0]
        side_x0 = 0
        if side_blanks is not None:
            blank_left, blank_right = side_blanks
            ratio = oriented.width / self.config.printer_width
            x0 = int(blank_left * ratio)
            x1 = oriented.width - int(blank_right * ratio)
            if x1 - x0 >= 8:
                side_trim = [blank_left, blank_right]
                side_x0 = x0
                oriented = oriented.crop((x0, 0, x1, oriented.height))
                scaled, scale_factor = scale_to_width(
                    oriented, self.config.printer_width
                )
                enhanced, thresholded, dithered = self._binarize(
                    scaled, analysis.page_type
                )
        stages["05_scaled.png"] = scaled
        stages["06_enhanced.png"] = enhanced.image
        stages["07_threshold.png"] = thresholded.image
        stages["08_dithered.png"] = dithered.image

        final_array = np.asarray(dithered.image.convert("L"), dtype=np.uint8)

        # Per-region dedicated pipelines (text / logo / photo) overwrite the
        # base result inside their bounding boxes with a fitting binarization.
        mapping = dict(
            angle=orientation.angle,
            cropped_size=cropped.size,
            side_x0=side_x0,
            scale=self.config.printer_width / max(1, oriented.width),
        )
        final_array = self._composite_content_regions(
            final_array, enhanced.image, regions, mapping
        )

        if analysis.page_type == "text":
            final_array = remove_isolated_noise(final_array, maximum_area=1)
        final_array, top_offset = crop_binary_vertical(
            final_array,
            margin=self.config.crop_margin,
            paper_trim=self.config.paper_trim,
        )

        # Machine-readable symbols are pasted last: regenerated from payload
        # when decodable, otherwise re-rasterized at high DPI and Otsu-only
        # thresholded. Never dithered, sharpened, CLAHE'd or gamma-corrected.
        final_array = self._composite_symbols(
            final_array,
            regions,
            mapping,
            top_offset=top_offset,
            page=page,
            crop_box=crop_box,
            pdf_bytes=pdf_bytes,
        )
        final = Image.fromarray(final_array, mode="L")
        bitmap = pack_1bpp(
            final,
            width=self.config.printer_width,
            minimum_rows=self.config.minimum_page_rows,
        )
        stages["09_final.png"] = bitmap.image

        if self.config.save_intermediates:
            for filename, image in stages.items():
                image.save(page_dir / filename, optimize=True)
        else:
            stages["09_final.png"].save(page_dir / "09_final.png", optimize=True)
        (page_dir / "bitmap.bin").write_bytes(bitmap.packed)

        report = {
            "page": page.number + 1,
            "source_size_pixels": list(page.image.size),
            "source_size_points": [page.width_points, page.height_points],
            "source_rotation": page.source_rotation,
            **analysis.to_dict(),
            "content_crop_box": list(crop_box),
            "chosen_orientation": orientation.angle,
            "orientation_scores": orientation.candidate_scores,
            "chosen_gamma": enhanced.gamma,
            "gamma_scores": enhanced.gamma_scores,
            "chosen_threshold_algorithm": thresholded.algorithm,
            "threshold_score": round(thresholded.score, 6),
            "threshold_candidate_scores": thresholded.candidate_scores,
            "chosen_dithering": dithered.algorithm,
            "dither_score": round(dithered.score, 6),
            "dither_candidate_scores": dithered.candidate_scores,
            "chosen_scaling_factor": round(scale_factor, 8),
            "side_blank_columns_removed": side_trim,
            "regions": [region.to_dict() for region in regions.all()],
            "symbols_detected": len(regions.symbols),
            "symbols_regenerated": sum(
                1 for region in regions.symbols if region.action == "regenerated"
            ),
            "final_width": bitmap.width,
            "final_height": bitmap.height,
            "bytes_per_row": bitmap.bytes_per_row,
            "packed_bytes": len(bitmap.packed),
            "paper_rows_removed": max(0, dithered.image.height - bitmap.height),
        }
        (page_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
        )
        return PageResult(page.number, bitmap, report, page_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a PDF into print-ready, quality-selected 1bpp pages"
    )
    parser.add_argument("pdf")
    parser.add_argument("--config", default="rendering.yaml")
    parser.add_argument("--name")
    args = parser.parse_args()

    pipeline = PDFPipeline.from_yaml(args.config)
    result = pipeline.process(args.pdf, job_name=args.name)
    print(result.report_path)
    for page in result.pages:
        print(
            f"page {page.page_number + 1}: "
            f"{page.bitmap.width}x{page.bitmap.height}, "
            f"{page.report['chosen_threshold_algorithm']}, "
            f"{page.report['chosen_dithering']}"
        )


if __name__ == "__main__":
    main()
