"""Threshold algorithms and automatic candidate comparison."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image
from skimage.filters import threshold_niblack, threshold_sauvola

from page_analyzer import binary_quality


@dataclass
class ThresholdResult:
    image: Image.Image
    algorithm: str
    score: float
    candidate_scores: dict[str, float]


def _window_size(gray: np.ndarray) -> int:
    size = max(15, min(75, min(gray.shape) // 12))
    return size if size % 2 else size + 1


def _wolf_threshold(gray: np.ndarray, window: int, k: float = 0.5) -> np.ndarray:
    source = gray.astype(np.float32)
    mean = cv2.boxFilter(source, -1, (window, window), normalize=True)
    square_mean = cv2.boxFilter(source * source, -1, (window, window), normalize=True)
    std = np.sqrt(np.maximum(square_mean - mean * mean, 0))
    max_std = max(float(std.max()), 1e-6)
    min_gray = float(source.min())
    threshold = mean + k * (std / max_std - 1.0) * (mean - min_gray)
    return np.where(source <= threshold, 0, 255).astype(np.uint8)


def apply_threshold(gray: np.ndarray, algorithm: str) -> np.ndarray:
    window = _window_size(gray)
    if algorithm == "otsu":
        return cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )[1]
    if algorithm == "adaptive":
        return cv2.adaptiveThreshold(
            gray,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            window,
            7,
        )
    if algorithm == "sauvola":
        value = threshold_sauvola(gray, window_size=window, k=0.2)
        return np.where(gray <= value, 0, 255).astype(np.uint8)
    if algorithm == "niblack":
        value = threshold_niblack(gray, window_size=window, k=-0.2)
        return np.where(gray <= value, 0, 255).astype(np.uint8)
    if algorithm == "wolf":
        return _wolf_threshold(gray, window)
    raise ValueError(f"unknown threshold algorithm: {algorithm}")


def choose_threshold(
    image: Image.Image,
    page_type: str,
    candidates: list[str],
    forced: str = "auto",
) -> ThresholdResult:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    algorithms = [forced] if forced != "auto" else candidates
    outputs: dict[str, np.ndarray] = {}
    scores: dict[str, float] = {}
    for algorithm in algorithms:
        output = apply_threshold(gray, algorithm)
        outputs[algorithm] = output
        score = binary_quality(output, gray, page_type)
        # Global Otsu is very stable for clean vector text; local methods are
        # favored only when their measured edge retention is better.
        if page_type == "text" and algorithm == "otsu":
            score += 0.025
        scores[algorithm] = score
    algorithm = max(algorithms, key=lambda name: scores[name])
    return ThresholdResult(
        image=Image.fromarray(outputs[algorithm], mode="L"),
        algorithm=algorithm,
        score=scores[algorithm],
        candidate_scores={
            key: round(value, 6) for key, value in scores.items()
        },
    )
