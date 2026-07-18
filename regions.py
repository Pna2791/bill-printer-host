"""Region detection and protected rendering for QR codes and barcodes.

Machine-readable symbols (QR, Code128, EAN, …) are detected before any
enhancement, excluded from CLAHE/gamma/sharpening/dithering, and rendered
through a dedicated geometry-preserving path:

- decodable symbols are REGENERATED from their payload at exact module
  geometry (covers vector-drawn and cleanly embedded symbols),
- undecodable symbols are re-thresholded from the unenhanced grayscale with
  Otsu and scaled with nearest-neighbor only,
- both get an enforced quiet zone and integer module-to-dot scaling.

Text, logo and photo regions are classified so the page compositor can apply
a dedicated threshold/dither per region.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

try:
    from pyzbar import pyzbar

    _PYZBAR_AVAILABLE = True
except ImportError:  # libzbar0 missing; QR detection falls back to OpenCV
    _PYZBAR_AVAILABLE = False

import barcode as pybarcode
import qrcode


# pyzbar symbology -> python-barcode generator name (None: cannot regenerate)
_SYMBOL_GENERATORS = {
    "CODE128": "code128",
    "CODE39": "code39",
    "EAN13": "ean13",
    "EAN8": "ean8",
    "UPCA": "upca",
    "I25": "itf",
    "ITF": "itf",
    "CODABAR": "codabar",
}


@dataclass
class Region:
    kind: str  # qr | barcode | text | logo | photo
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 in detection-image pixels
    payload: str = ""
    symbol_type: str = ""
    decoded: bool = False
    action: str = ""  # regenerated | rethresholded | region-threshold | region-dither

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "bbox": list(self.bbox),
            "payload": self.payload if len(self.payload) <= 128 else
            self.payload[:125] + "...",
            "symbol_type": self.symbol_type,
            "decoded": self.decoded,
            "action": self.action,
        }


@dataclass
class RegionSet:
    symbols: list[Region] = field(default_factory=list)  # qr + barcode
    texts: list[Region] = field(default_factory=list)
    photos: list[Region] = field(default_factory=list)
    logos: list[Region] = field(default_factory=list)

    def all(self) -> list[Region]:
        return [*self.symbols, *self.texts, *self.photos, *self.logos]


def _clamp_bbox(
    x0: int, y0: int, x1: int, y1: int, width: int, height: int, pad: int = 0
) -> tuple[int, int, int, int]:
    return (
        max(0, x0 - pad),
        max(0, y0 - pad),
        min(width, x1 + pad),
        min(height, y1 + pad),
    )


def detect_symbols(gray: np.ndarray) -> list[Region]:
    """Find QR codes and 1-D barcodes on the unenhanced grayscale page."""
    regions: list[Region] = []
    h, w = gray.shape

    # zbar performs poorly on very large images; detect on a bounded copy and
    # map coordinates back. Decoding also re-runs per-ROI later if needed.
    scale = 1.0
    detect_img = gray
    longest = max(h, w)
    if longest > 2200:
        scale = 2200.0 / longest
        detect_img = cv2.resize(
            gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )

    seen: list[tuple[int, int, int, int]] = []
    if _PYZBAR_AVAILABLE:
        for result in pyzbar.decode(detect_img):
            x0 = int(result.rect.left / scale)
            y0 = int(result.rect.top / scale)
            x1 = int((result.rect.left + result.rect.width) / scale)
            y1 = int((result.rect.top + result.rect.height) / scale)
            bbox = _clamp_bbox(x0, y0, x1, y1, w, h)
            kind = "qr" if result.type == "QRCODE" else "barcode"
            try:
                payload = result.data.decode("utf-8")
            except UnicodeDecodeError:
                payload = ""
            regions.append(
                Region(
                    kind=kind,
                    bbox=bbox,
                    payload=payload,
                    symbol_type=result.type,
                    decoded=bool(payload),
                )
            )
            seen.append(bbox)

    # OpenCV catches QR codes zbar misses (e.g. inverted or low-contrast).
    detector = cv2.QRCodeDetector()
    try:
        ok, decoded_list, points, _ = detector.detectAndDecodeMulti(detect_img)
    except cv2.error:
        ok = False
    if ok and points is not None:
        for text, quad in zip(decoded_list, points):
            xs = quad[:, 0] / scale
            ys = quad[:, 1] / scale
            bbox = _clamp_bbox(
                int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()), w, h
            )
            if any(_overlap_fraction(bbox, other) > 0.4 for other in seen):
                continue
            regions.append(
                Region(
                    kind="qr",
                    bbox=bbox,
                    payload=text,
                    symbol_type="QRCODE",
                    decoded=bool(text),
                )
            )
            seen.append(bbox)
    return regions


def _overlap_fraction(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int]
) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    smaller = min(
        (a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1])
    )
    return inter / max(1, smaller)


def classify_image_blocks(
    gray: np.ndarray,
    image_blocks: list[dict],
    points_to_pixels: float,
    offset: tuple[int, int],
    symbol_boxes: list[tuple[int, int, int, int]],
) -> tuple[list[Region], list[Region]]:
    """Split PDF image blocks into photos (real midtones) and logos."""
    photos: list[Region] = []
    logos: list[Region] = []
    h, w = gray.shape
    for block in image_blocks:
        bx0, by0, bx1, by1 = block["bbox"]
        x0 = int(bx0 * points_to_pixels) - offset[0]
        y0 = int(by0 * points_to_pixels) - offset[1]
        x1 = int(bx1 * points_to_pixels) - offset[0]
        y1 = int(by1 * points_to_pixels) - offset[1]
        bbox = _clamp_bbox(x0, y0, x1, y1, w, h)
        if bbox[2] - bbox[0] < 8 or bbox[3] - bbox[1] < 8:
            continue
        if any(_overlap_fraction(bbox, s) > 0.5 for s in symbol_boxes):
            continue
        roi = gray[bbox[1] : bbox[3], bbox[0] : bbox[2]]
        midtones = float(np.mean((roi > 64) & (roi < 192)))
        region = Region(kind="photo" if midtones > 0.2 else "logo", bbox=bbox)
        (photos if region.kind == "photo" else logos).append(region)
    return photos, logos


def detect_regions(
    image: Image.Image,
    text_blocks: list[dict],
    image_blocks: list[dict],
    points_to_pixels: float,
    crop_offset: tuple[int, int],
) -> RegionSet:
    """Detect all regions on the cropped, oriented, UNENHANCED page image.

    Runs before enhancement so detection sees faithful geometry, and so the
    compositor can exempt machine-readable symbols from destructive filters.
    """
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    h, w = gray.shape
    symbols = detect_symbols(gray)
    symbol_boxes = [region.bbox for region in symbols]

    texts: list[Region] = []
    for block in text_blocks:
        bx0, by0, bx1, by1 = block["bbox"]
        x0 = int(bx0 * points_to_pixels) - crop_offset[0]
        y0 = int(by0 * points_to_pixels) - crop_offset[1]
        x1 = int(bx1 * points_to_pixels) - crop_offset[0]
        y1 = int(by1 * points_to_pixels) - crop_offset[1]
        bbox = _clamp_bbox(x0, y0, x1, y1, w, h)
        if bbox[2] - bbox[0] < 4 or bbox[3] - bbox[1] < 4:
            continue
        if any(_overlap_fraction(bbox, s) > 0.5 for s in symbol_boxes):
            continue
        texts.append(Region(kind="text", bbox=bbox))

    photos, logos = classify_image_blocks(
        gray, image_blocks, points_to_pixels, crop_offset, symbol_boxes
    )
    return RegionSet(symbols=symbols, texts=texts, photos=photos, logos=logos)


def map_bbox_to_final(
    bbox: tuple[int, int, int, int],
    angle: int,
    cropped_size: tuple[int, int],
    side_x0: int,
    scale: float,
    top_offset: int,
) -> tuple[int, int, int, int]:
    """Map a bbox in cropped-page pixels to final-bitmap dots.

    Applies (verified against PIL) the CCW expand-rotation, the horizontal
    side-blank re-crop, the LANCZOS scale factor, and the vertical paper trim.
    """
    x0, y0, x1, y1 = bbox
    width, height = cropped_size
    corners = [(x0, y0), (x1 - 1, y0), (x0, y1 - 1), (x1 - 1, y1 - 1)]
    mapped = []
    for x, y in corners:
        if angle == 0:
            mx, my = x, y
        elif angle == 90:
            mx, my = y, width - 1 - x
        elif angle == 180:
            mx, my = width - 1 - x, height - 1 - y
        else:  # 270
            mx, my = height - 1 - y, x
        mapped.append((mx, my))
    xs = [m[0] for m in mapped]
    ys = [m[1] for m in mapped]
    fx0 = int(round((min(xs) - side_x0) * scale))
    fx1 = int(round((max(xs) + 1 - side_x0) * scale))
    fy0 = int(round(min(ys) * scale)) - top_offset
    fy1 = int(round((max(ys) + 1) * scale)) - top_offset
    return fx0, fy0, fx1, fy1


# ----- protected symbol rendering ------------------------------------------

def regenerate_qr(
    payload: str,
    bbox_dots: int,
    page_width_dots: int,
    quiet_modules: int = 4,
    min_module_dots: int = 2,
) -> np.ndarray | None:
    """Rebuild a QR from its payload with integer module scaling.

    Module size tracks the original symbol size; the quiet zone may extend
    beyond the detected bbox as long as the page can hold it. Returns a uint8
    array (0 ink / 255 white) including the quiet zone, or None when the page
    cannot hold the symbol at min_module_dots per module.
    """
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=0,
        box_size=1,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = np.array(qr.get_matrix(), dtype=bool)
    modules = matrix.shape[0]
    total_modules = modules + 2 * quiet_modules

    # Prefer fitting symbol + quiet zone inside the original footprint so the
    # cleared quiet zone never destroys neighboring content. Only expand when
    # modules would otherwise drop below the scannability minimum.
    dots_per_module = (bbox_dots + 8) // total_modules
    if dots_per_module < min_module_dots:
        dots_per_module = min_module_dots
    if total_modules * dots_per_module > page_width_dots:
        return None

    canvas_modules = np.zeros((total_modules, total_modules), dtype=bool)
    canvas_modules[
        quiet_modules : quiet_modules + modules,
        quiet_modules : quiet_modules + modules,
    ] = matrix
    scaled = np.kron(
        canvas_modules, np.ones((dots_per_module, dots_per_module), dtype=bool)
    )
    return np.where(scaled, 0, 255).astype(np.uint8)


def regenerate_barcode(
    payload: str,
    symbol_type: str,
    bbox_width_dots: int,
    bbox_height_dots: int,
    page_width_dots: int,
    quiet_modules: int = 10,
) -> np.ndarray | None:
    """Rebuild a 1-D barcode from its payload with integer module scaling.

    1-D quiet zones are conventionally 10 narrow modules on each side; they
    are shrunk (never below 4) only when the page is too narrow.
    """
    generator_name = _SYMBOL_GENERATORS.get(symbol_type)
    if generator_name is None or not payload:
        return None
    try:
        generator = pybarcode.get_barcode_class(generator_name)
        pattern = generator(payload).build()[0]
    except Exception:
        return None
    modules = np.array([c == "1" for c in pattern], dtype=bool)
    count = len(modules)

    dots_per_module = max(1, round(bbox_width_dots / count))
    while dots_per_module > 1 and (
        (count + 2 * quiet_modules) * dots_per_module > page_width_dots
    ):
        dots_per_module -= 1
    if (count + 2 * quiet_modules) * dots_per_module > page_width_dots:
        quiet_modules = max(4, (page_width_dots // dots_per_module - count) // 2)
    total_modules = count + 2 * quiet_modules
    if total_modules * dots_per_module > page_width_dots:
        return None

    row = np.zeros(total_modules, dtype=bool)
    row[quiet_modules : quiet_modules + count] = modules
    scaled_row = np.repeat(row, dots_per_module)
    height = max(16, bbox_height_dots)
    bars = np.tile(scaled_row, (height, 1))
    return np.where(bars, 0, 255).astype(np.uint8)


def rethreshold_symbol(
    source_gray: np.ndarray, target_width: int, target_height: int
) -> np.ndarray:
    """Geometry-preserving fallback for undecodable symbols.

    Otsu on the unenhanced grayscale, then a single nearest-neighbor resize.
    No CLAHE, gamma, sharpening or dithering is ever applied here.
    """
    _, binary = cv2.threshold(
        source_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return cv2.resize(
        binary, (target_width, target_height), interpolation=cv2.INTER_NEAREST
    )
