"""Error-diffusion/ordered dithering with content-aware comparison."""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from page_analyzer import binary_quality


@dataclass
class DitherResult:
    image: Image.Image
    algorithm: str
    score: float
    candidate_scores: dict[str, float]


KERNELS: dict[str, tuple[list[tuple[int, int, float]], float]] = {
    "floyd_steinberg": (
        [(1, 0, 7), (-1, 1, 3), (0, 1, 5), (1, 1, 1)],
        16,
    ),
    "jarvis": (
        [
            (1, 0, 7), (2, 0, 5),
            (-2, 1, 3), (-1, 1, 5), (0, 1, 7), (1, 1, 5), (2, 1, 3),
            (-2, 2, 1), (-1, 2, 3), (0, 2, 5), (1, 2, 3), (2, 2, 1),
        ],
        48,
    ),
    "stucki": (
        [
            (1, 0, 8), (2, 0, 4),
            (-2, 1, 2), (-1, 1, 4), (0, 1, 8), (1, 1, 4), (2, 1, 2),
            (-2, 2, 1), (-1, 2, 2), (0, 2, 4), (1, 2, 2), (2, 2, 1),
        ],
        42,
    ),
    "atkinson": (
        [(1, 0, 1), (2, 0, 1), (-1, 1, 1), (0, 1, 1), (1, 1, 1), (0, 2, 1)],
        8,
    ),
}


def error_diffusion(gray: np.ndarray, algorithm: str) -> np.ndarray:
    kernel, divisor = KERNELS[algorithm]
    work = gray.astype(np.float32).copy()
    height, width = work.shape
    output = np.empty_like(gray)
    for y in range(height):
        # Serpentine scan reduces directional artifacts.
        reverse = bool(y % 2)
        xs = range(width - 1, -1, -1) if reverse else range(width)
        for x in xs:
            old = work[y, x]
            new = 255.0 if old >= 128 else 0.0
            output[y, x] = int(new)
            error = old - new
            for dx, dy, weight in kernel:
                target_x = x - dx if reverse else x + dx
                target_y = y + dy
                if 0 <= target_x < width and target_y < height:
                    work[target_y, target_x] = np.clip(
                        work[target_y, target_x] + error * weight / divisor,
                        0,
                        255,
                    )
    return output


def ordered_bayer(gray: np.ndarray) -> np.ndarray:
    bayer8 = np.array(
        [
            [0, 48, 12, 60, 3, 51, 15, 63],
            [32, 16, 44, 28, 35, 19, 47, 31],
            [8, 56, 4, 52, 11, 59, 7, 55],
            [40, 24, 36, 20, 43, 27, 39, 23],
            [2, 50, 14, 62, 1, 49, 13, 61],
            [34, 18, 46, 30, 33, 17, 45, 29],
            [10, 58, 6, 54, 9, 57, 5, 53],
            [42, 26, 38, 22, 41, 25, 37, 21],
        ],
        dtype=np.float32,
    )
    threshold = (bayer8 + 0.5) * 255.0 / 64.0
    tiled = np.tile(
        threshold,
        (
            int(np.ceil(gray.shape[0] / 8)),
            int(np.ceil(gray.shape[1] / 8)),
        ),
    )[: gray.shape[0], : gray.shape[1]]
    return np.where(gray > tiled, 255, 0).astype(np.uint8)


def _tone_score(binary: np.ndarray, gray: np.ndarray) -> float:
    # Thermal dots integrate spatially. Compare local averages rather than
    # individual pixels to reward faithful perceived tones.
    reconstructed = cv2.GaussianBlur(binary, (0, 0), 2.0).astype(np.float32)
    reference = cv2.GaussianBlur(gray, (0, 0), 2.0).astype(np.float32)
    mse = float(np.mean((reconstructed - reference) ** 2))
    return max(0.0, 1.0 - mse / (255.0**2))


def choose_dither(
    enhanced: Image.Image,
    thresholded: Image.Image,
    page_type: str,
    candidates: list[str],
    forced: str = "auto",
) -> DitherResult:
    gray = np.asarray(enhanced.convert("L"), dtype=np.uint8)
    threshold_array = np.asarray(thresholded.convert("L"), dtype=np.uint8)
    algorithms = [forced] if forced != "auto" else candidates
    # Candidate comparison needs representative structure and tone, not every
    # row of a long receipt. Evaluate all algorithms on a deterministic,
    # width-preserving sample; render only the winner at full resolution.
    if gray.shape[0] > 192:
        sample_rows = np.linspace(0, gray.shape[0] - 1, 192).astype(np.int32)
        sample_gray = gray[sample_rows]
        sample_threshold = threshold_array[sample_rows]
    else:
        sample_gray = gray
        sample_threshold = threshold_array
    scores: dict[str, float] = {}
    # Measure how bilevel the page actually is. Labels, forms and scanned text
    # are often classified "photo" because they arrive as raster images, but
    # they contain almost no midtones; dithering them only adds speckle around
    # glyphs and barcodes. Trust the histogram over the block-based page type.
    midtone_fraction = float(np.mean((sample_gray > 64) & (sample_gray < 192)))
    effectively_bilevel = midtone_fraction < 0.12
    priors = {
        "text": {"none": 0.10},
        "photo": {"floyd_steinberg": 0.035, "jarvis": 0.02},
        "graphics": {"atkinson": 0.04, "none": 0.015},
    }
    if effectively_bilevel:
        priors = {key: {"none": 0.10} for key in priors}
    for algorithm in algorithms:
        if algorithm == "none":
            output = sample_threshold
        elif algorithm == "bayer":
            output = ordered_bayer(sample_gray)
        elif algorithm in KERNELS:
            output = error_diffusion(sample_gray, algorithm)
        else:
            raise ValueError(f"unknown dither algorithm: {algorithm}")
        structural = binary_quality(output, sample_gray, page_type)
        tonal = _tone_score(output, sample_gray)
        if effectively_bilevel or page_type == "text":
            tonal_weight = 0.18
        else:
            tonal_weight = 0.55
        score = structural * (1.0 - tonal_weight) + tonal * tonal_weight
        score += priors.get(page_type, {}).get(algorithm, 0.0)
        scores[algorithm] = score
    algorithm = max(algorithms, key=lambda name: scores[name])
    if algorithm == "none":
        winner = threshold_array
    elif algorithm == "bayer":
        winner = ordered_bayer(gray)
    else:
        winner = error_diffusion(gray, algorithm)
    return DitherResult(
        image=Image.fromarray(winner, mode="L"),
        algorithm=algorithm,
        score=scores[algorithm],
        candidate_scores={
            key: round(value, 6) for key, value in scores.items()
        },
    )
