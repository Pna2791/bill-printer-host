"""Page composition and quality metrics used by automatic selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass

import cv2
import numpy as np
from PIL import Image

from pdf_loader import PDFPage


@dataclass
class PageAnalysis:
    content_bbox: tuple[int, int, int, int]
    content_coverage: float
    text_coverage: float
    image_coverage: float
    estimated_sharpness: float
    estimated_readability: float
    page_type: str
    foreground_ratio: float
    edge_density: float
    dynamic_range: float

    def to_dict(self) -> dict:
        result = asdict(self)
        result["content_bbox"] = list(self.content_bbox)
        return result


def pil_gray(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("L"), dtype=np.uint8)


def visible_content_mask(gray: np.ndarray, white_threshold: int = 248) -> np.ndarray:
    # Include pale content while rejecting JPEG noise in nominally white margins.
    delta = 255 - gray
    mask = (gray < white_threshold).astype(np.uint8) * 255
    local_variance = cv2.GaussianBlur(delta.astype(np.float32) ** 2, (0, 0), 2)
    local_mean = cv2.GaussianBlur(delta.astype(np.float32), (0, 0), 2)
    variance = np.maximum(0, local_variance - local_mean**2)
    mask[variance > 5.0] = 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    points = cv2.findNonZero(mask)
    h, w = mask.shape
    if points is None:
        return (0, 0, w, h)
    x, y, bw, bh = cv2.boundingRect(points)
    return (x, y, x + bw, y + bh)


def _metadata_coverage(blocks: list[dict], page: PDFPage) -> float:
    page_area = max(1.0, page.width_points * page.height_points)
    area = 0.0
    for block in blocks:
        x0, y0, x1, y1 = block["bbox"]
        area += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return min(1.0, area / page_area)


def analyze_page(
    page: PDFPage,
    image: Image.Image | None = None,
    white_threshold: int = 248,
    metadata_area_fraction: float = 1.0,
) -> PageAnalysis:
    image = image or page.image
    gray = pil_gray(image)
    h, w = gray.shape
    mask = visible_content_mask(gray, white_threshold)
    bbox = mask_bbox(mask)
    x0, y0, x1, y1 = bbox
    bbox_area = max(1, (x1 - x0) * (y1 - y0))
    page_area = max(1, w * h)
    content_coverage = float(np.count_nonzero(mask)) / page_area

    # PDF block metadata is measured against the full page. When analyzing a
    # cropped image, rescale so coverage stays comparable.
    area_fraction = max(1e-6, min(1.0, metadata_area_fraction))
    text_coverage = min(1.0, _metadata_coverage(page.text_blocks, page) / area_fraction)
    image_coverage = min(
        1.0, _metadata_coverage(page.image_blocks, page) / area_fraction
    )

    # Scanned pages have no PDF text metadata. Estimate text-like regions from
    # many small, high-contrast connected components and horizontal edges.
    edges = cv2.Canny(gray, 70, 180)
    edge_density = float(np.count_nonzero(edges)) / page_area
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
    components = stats[1:] if count > 1 else np.empty((0, 5), dtype=np.int32)
    small_components = sum(
        1
        for x, y, cw, ch, area in components
        if 3 <= area <= page_area * 0.002 and cw > 1 and ch > 1
    )
    text_likelihood = min(1.0, small_components / max(20.0, bbox_area / 6000.0))
    if not page.text_blocks:
        text_coverage = content_coverage * text_likelihood
    if not page.image_blocks:
        image_coverage = max(0.0, content_coverage - text_coverage)

    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharpness = min(1.0, np.log1p(lap_var) / np.log(2001.0))
    p1, p99 = np.percentile(gray, [1, 99])
    dynamic_range = float(p99 - p1) / 255.0
    readability = min(
        1.0,
        0.45 * sharpness
        + 0.35 * min(1.0, edge_density / 0.12)
        + 0.20 * dynamic_range,
    )
    foreground_ratio = float(np.count_nonzero(gray < 128)) / page_area

    if text_coverage >= max(0.03, image_coverage * 1.4) or text_likelihood > 0.62:
        page_type = "text"
    elif image_coverage > max(0.08, text_coverage * 1.4):
        page_type = "photo"
    else:
        page_type = "graphics"

    return PageAnalysis(
        content_bbox=bbox,
        content_coverage=round(content_coverage * 100, 3),
        text_coverage=round(text_coverage * 100, 3),
        image_coverage=round(image_coverage * 100, 3),
        estimated_sharpness=round(sharpness, 4),
        estimated_readability=round(readability, 4),
        page_type=page_type,
        foreground_ratio=round(foreground_ratio, 5),
        edge_density=round(edge_density, 5),
        dynamic_range=round(dynamic_range, 4),
    )


def binary_quality(
    binary: np.ndarray, reference_gray: np.ndarray, page_type: str
) -> float:
    """Score a binary candidate without claiming OCR-level readability.

    Rewards edge agreement, connected glyph-like structures and usable ink
    density; penalizes isolated speckle, solid blobs and lost edge detail.
    """
    if binary.ndim != 2:
        raise ValueError("binary_quality expects a 2-D array")
    ink = binary < 128
    ink_ratio = float(ink.mean())
    ref_edges = cv2.Canny(reference_gray, 60, 170) > 0
    out_edges = cv2.Canny(binary, 60, 170) > 0
    union = np.count_nonzero(ref_edges | out_edges)
    edge_iou = (
        float(np.count_nonzero(ref_edges & out_edges)) / union if union else 1.0
    )

    count, _labels, stats, _ = cv2.connectedComponentsWithStats(
        ink.astype(np.uint8), connectivity=8
    )
    areas = stats[1:, cv2.CC_STAT_AREA] if count > 1 else np.array([])
    isolated = float(np.count_nonzero(areas <= 2)) / max(1, len(areas))
    largest = float(areas.max()) / binary.size if len(areas) else 0.0

    # Ink density should track the actual document, not a fixed per-type
    # target: labels/logos are legitimately much darker than body text.
    reference_ink = float(np.mean(reference_gray < 128))
    density_score = max(0.0, 1.0 - abs(ink_ratio - reference_ink) / 0.25)

    # Solid dark fills (barcodes, logos, filled arrows) must stay solid. Where
    # the source is strongly dark, the binary output should also be inked;
    # adaptive/Niblack tend to hollow these out and are penalized here.
    solid_source = reference_gray < 64
    solid_pixels = int(np.count_nonzero(solid_source))
    if solid_pixels:
        fill_retention = float(np.count_nonzero(ink & solid_source)) / solid_pixels
    else:
        fill_retention = 1.0

    score = (
        0.42 * edge_iou
        + 0.20 * density_score
        + 0.14 * (1.0 - isolated)
        + 0.24 * fill_retention
    )
    if largest > 0.75:
        score -= (largest - 0.75) * 0.8
    return float(max(0.0, min(1.0, score)))
