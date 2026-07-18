"""Automatic orientation selection for maximum useful print scale."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from pdf_loader import PDFPage


@dataclass
class OrientationResult:
    image: Image.Image
    angle: int
    score: float
    scaling_factor: float
    candidate_scores: dict[str, float]


def _rotate(image: Image.Image, angle: int) -> Image.Image:
    if angle == 0:
        return image.copy()
    # PIL rotates counter-clockwise. expand=True prevents content clipping.
    return image.rotate(angle, expand=True, fillcolor=255)


def _horizontal_structure(gray: np.ndarray) -> float:
    """Estimate upright text lines; useful for scanned documents."""
    edges = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    horizontal = float(np.mean(np.abs(edges)))
    vertical_edges = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    vertical = float(np.mean(np.abs(vertical_edges)))
    total = horizontal + vertical
    return horizontal / total if total else 0.5


def choose_orientation(
    image: Image.Image,
    page: PDFPage,
    printer_width: int,
    mode: str = "auto",
) -> OrientationResult:
    if mode != "auto":
        angle = int(mode)
        rotated = _rotate(image, angle)
        scale = printer_width / rotated.width
        return OrientationResult(rotated, angle, scale, scale, {mode: scale})

    scores: dict[str, float] = {}
    candidates: dict[int, Image.Image] = {}
    for angle in (0, 90, 180, 270):
        candidate = _rotate(image, angle)
        candidates[angle] = candidate
        scale = printer_width / max(1, candidate.width)
        gray = np.asarray(candidate.convert("L"), dtype=np.uint8)
        structure = _horizontal_structure(gray)

        # Scale dominates: the orientation using the narrow source dimension
        # maps more source pixels to every printer dot. Text structure avoids
        # turning an already upright landscape document sideways unnecessarily.
        upright_prior = 1.0 if angle == 0 else (0.995 if angle == 180 else 0.98)
        structure_bonus = 0.08 * structure
        scores[str(angle)] = scale * (upright_prior + structure_bonus)

    angle = max((0, 90, 180, 270), key=lambda value: scores[str(value)])
    # 0° and 180°, or 90° and 270°, have equal scale. Without OCR there is no
    # defensible way to infer upside-down text, so preserve source direction.
    if angle == 180 and abs(scores["180"] - scores["0"]) < 0.02 * scores["0"]:
        angle = 0
    if angle == 270 and abs(scores["270"] - scores["90"]) < 0.02 * scores["90"]:
        angle = 90
    result = candidates[angle]
    return OrientationResult(
        image=result,
        angle=angle,
        score=scores[str(angle)],
        scaling_factor=printer_width / max(1, result.width),
        candidate_scores={key: round(value, 6) for key, value in scores.items()},
    )
