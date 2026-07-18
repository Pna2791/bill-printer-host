"""Final scaling, byte alignment and MXW01-compatible bitmap packing."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass
class RenderedBitmap:
    image: Image.Image
    packed: bytes
    width: int
    height: int
    bytes_per_row: int


def scale_to_width(image: Image.Image, width: int) -> tuple[Image.Image, float]:
    if width <= 0:
        raise ValueError("width must be positive")
    factor = width / max(1, image.width)
    height = max(1, round(image.height * factor))
    return image.resize((width, height), Image.Resampling.LANCZOS), factor


def ensure_exact_width(binary: Image.Image, width: int) -> Image.Image:
    image = binary.convert("L")
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.Resampling.NEAREST)
    if image.width == width:
        return image
    canvas = Image.new("L", (width, image.height), 255)
    x = (width - image.width) // 2
    canvas.paste(image, (x, 0))
    return canvas


def pack_1bpp(binary: Image.Image, width: int, minimum_rows: int = 1) -> RenderedBitmap:
    if width % 8:
        raise ValueError("printer width must be a multiple of 8")
    image = ensure_exact_width(binary, width)
    array = np.asarray(image, dtype=np.uint8)
    if array.shape[0] < minimum_rows:
        padding = np.full((minimum_rows - array.shape[0], width), 255, dtype=np.uint8)
        array = np.vstack([array, padding])
        image = Image.fromarray(array, mode="L")

    black = array < 128
    bytes_per_row = width // 8
    reshaped = black.reshape(array.shape[0], bytes_per_row, 8)
    weights = (1 << np.arange(8, dtype=np.uint8)).reshape(1, 1, 8)
    packed = np.sum(reshaped * weights, axis=2, dtype=np.uint16).astype(np.uint8)
    return RenderedBitmap(
        image=image,
        packed=packed.tobytes(),
        width=width,
        height=array.shape[0],
        bytes_per_row=bytes_per_row,
    )
