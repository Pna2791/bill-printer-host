"""Content-safe border and trailing-paper cropping."""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from page_analyzer import pil_gray, visible_content_mask


def robust_mask_bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    """Content bounding box that ignores stray specks near the page edges.

    Tiny isolated components (dust, scan noise) are discarded before taking
    the bounding box; otherwise a single speck keeps the whole margin alive.
    Thin but large structures (hairlines, table borders) survive because their
    total component area is well above the floor.
    """
    ink = (mask > 0).astype(np.uint8)
    h, w = ink.shape
    # ~16 px at 600 DPI (0.03 mm²); scaled down for small images.
    minimum_area = max(6, int(h * w * 2e-6))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    keep = [
        label
        for label in range(1, count)
        if stats[label, cv2.CC_STAT_AREA] >= minimum_area
    ]
    if not keep:
        return (0, 0, w, h)
    x0, y0, x1, y1 = w, h, 0, 0
    for label in keep:
        x = stats[label, cv2.CC_STAT_LEFT]
        y = stats[label, cv2.CC_STAT_TOP]
        x0 = min(x0, x)
        y0 = min(y0, y)
        x1 = max(x1, x + stats[label, cv2.CC_STAT_WIDTH])
        y1 = max(y1, y + stats[label, cv2.CC_STAT_HEIGHT])
    return (int(x0), int(y0), int(x1), int(y1))


def crop_visible_content(
    image: Image.Image,
    white_threshold: int = 248,
    margin: int = 4,
    safety_margin: int = 2,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    gray = pil_gray(image)
    # Bounding box uses the raw threshold, not visible_content_mask: its
    # variance channel inflates dust specks into blobs too big to filter out.
    raw_ink = ((gray < white_threshold).astype(np.uint8)) * 255
    x0, y0, x1, y1 = robust_mask_bbox(raw_ink)
    pad = max(0, margin + safety_margin)
    x0 = max(0, x0 - pad)
    y0 = max(0, y0 - pad)
    x1 = min(image.width, x1 + pad)
    y1 = min(image.height, y1 + pad)
    return image.crop((x0, y0, x1, y1)), (x0, y0, x1, y1)


def crop_binary_vertical(
    binary: np.ndarray, margin: int = 2, paper_trim: bool = True
) -> tuple[np.ndarray, int]:
    """Remove blank rows while preserving horizontal width for byte alignment.

    Returns the trimmed array and the number of rows removed from the top so
    later compositing can convert coordinates.
    """
    if not paper_trim:
        return binary, 0
    ink = binary < 128
    row_has_ink = np.any(ink, axis=1)
    indices = np.flatnonzero(row_has_ink)
    if len(indices) == 0:
        return binary[:1].copy(), 0
    first = max(0, int(indices[0]) - margin)
    last = min(binary.shape[0], int(indices[-1]) + margin + 1)
    return binary[first:last].copy(), first


def remove_isolated_noise(binary: np.ndarray, maximum_area: int = 2) -> np.ndarray:
    ink = (binary < 128).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    clean = ink.copy()
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] <= maximum_area:
            clean[labels == label] = 0
    return np.where(clean > 0, 0, 255).astype(np.uint8)
