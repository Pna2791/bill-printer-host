"""Conservative grayscale enhancement for 203-DPI thermal output."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageEnhance, ImageOps

from render_config import EnhancementConfig


@dataclass
class EnhancementResult:
    image: Image.Image
    gamma: float
    gamma_scores: dict[str, float]


def _apply_gamma(gray: np.ndarray, gamma: float) -> np.ndarray:
    # gamma < 1 lightens shadows; gamma > 1 strengthens dark strokes.
    table = np.array(
        [((value / 255.0) ** gamma) * 255 for value in range(256)],
        dtype=np.uint8,
    )
    return cv2.LUT(gray, table)


def _gamma_score(image: np.ndarray, page_type: str) -> float:
    p2, p98 = np.percentile(image, [2, 98])
    contrast = float(p98 - p2) / 255.0
    clipped = float(np.mean((image <= 2) | (image >= 253)))
    lap = min(1.0, float(cv2.Laplacian(image, cv2.CV_64F).var()) / 900.0)
    histogram = cv2.calcHist([image], [0], None, [256], [0, 256]).ravel()
    probabilities = histogram[histogram > 0] / image.size
    entropy = float(-(probabilities * np.log2(probabilities)).sum()) / 8.0
    if page_type == "text":
        return 0.55 * contrast + 0.35 * lap + 0.10 * entropy - 0.15 * clipped
    return 0.35 * contrast + 0.25 * lap + 0.40 * entropy - 0.25 * clipped


def enhance(
    image: Image.Image, page_type: str, config: EnhancementConfig
) -> EnhancementResult:
    pil_gray = image.convert("L")
    if config.auto_contrast:
        pil_gray = ImageOps.autocontrast(pil_gray, cutoff=0.5)
    if config.contrast != 1.0:
        pil_gray = ImageEnhance.Contrast(pil_gray).enhance(config.contrast)
    gray = np.asarray(pil_gray, dtype=np.uint8)

    if config.denoise_strength > 0:
        if page_type == "text":
            # Median filtering removes isolated scan noise without averaging edges.
            kernel = 3 if config.denoise_strength <= 4 else 5
            gray = cv2.medianBlur(gray, kernel)
        else:
            gray = cv2.bilateralFilter(
                gray,
                d=5,
                sigmaColor=15 + config.denoise_strength * 4,
                sigmaSpace=5,
            )

    if config.clahe:
        clahe = cv2.createCLAHE(
            clipLimit=config.clahe_clip_limit,
            tileGridSize=(config.clahe_grid_size, config.clahe_grid_size),
        )
        clahe_result = clahe.apply(gray)
        # Full CLAHE can amplify scan noise. Blend conservatively.
        weight = 0.35 if page_type == "text" else 0.65
        gray = cv2.addWeighted(clahe_result, weight, gray, 1.0 - weight, 0)

    candidates = (
        [float(config.gamma)]
        if config.gamma != "auto"
        else [float(value) for value in config.gamma_candidates]
    )
    gamma_images = {gamma: _apply_gamma(gray, gamma) for gamma in candidates}
    gamma_scores = {
        gamma: _gamma_score(candidate, page_type)
        for gamma, candidate in gamma_images.items()
    }
    gamma = max(candidates, key=lambda value: gamma_scores[value])
    gray = gamma_images[gamma]

    if config.sharpness > 0:
        blurred = cv2.GaussianBlur(gray, (0, 0), 0.8)
        amount = min(1.4, config.sharpness * (0.65 if page_type == "text" else 0.4))
        gray = cv2.addWeighted(gray, 1.0 + amount, blurred, -amount, 0)

    if config.morphological_cleanup and page_type == "text":
        # Closing only very small gaps strengthens glyph boundaries without
        # visibly emboldening normal strokes.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1))
        dark = 255 - gray
        closed = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, kernel)
        gray = 255 - cv2.addWeighted(closed, 0.25, dark, 0.75, 0)

    return EnhancementResult(
        image=Image.fromarray(gray, mode="L"),
        gamma=gamma,
        gamma_scores={
            str(key): round(value, 6) for key, value in gamma_scores.items()
        },
    )
