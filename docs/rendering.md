# High-quality PDF rendering

The renderer treats every PDF page independently and prioritizes printed
content size and legibility over runtime.

## Usage

```bash
python pipeline.py document.pdf --config rendering.yaml --name receipt
```

Outputs are stored under `render_output/<name>_<sha>/`:

```text
report.json
page_0001/
  01_original.png
  02_rendered.png
  03_cropped.png
  04_rotated.png
  05_scaled.png
  06_enhanced.png
  07_threshold.png
  08_dithered.png
  09_final.png
  bitmap.bin
  report.json
```

`bitmap.bin` is already packed as LSB-left, black-is-1 rows for the configured
printer width. No image processing occurs in the Bluetooth layer.

## Pipeline

1. PyMuPDF renders each page at 300–600 DPI (600 by default). The high-DPI
   raster and PDF block metadata are cached by PDF SHA-256 and DPI.
2. Margins are cropped immediately after rasterization. The bounding box
   ignores tiny isolated specks (dust, scan noise) so a stray pixel cannot
   keep a blank margin alive; hairlines and pale content are preserved.
3. Regions are detected on the cropped, unenhanced raster: QR codes and 1-D
   barcodes (zbar + OpenCV), and text/logo/photo areas from PDF block
   metadata (logos vs photos split by midtone histogram).
4. PDF text/image blocks and image connected components classify the page as
   text, photo, or graphics.
5. 0°, 90°, 180°, and 270° are scored. Printable scale dominates, with a
   horizontal-structure/readability term and source-direction tie-breaking.
6. The selected orientation is resized exactly once with LANCZOS.
7. Auto contrast, conservative CLAHE, edge-preserving denoise, gamma candidate
   selection, and unsharp masking prepare the 203-DPI image.
8. Otsu, adaptive Gaussian, Sauvola, Niblack, and Wolf thresholds are scored.
9. No dither, Floyd–Steinberg, Jarvis, Stucki, Atkinson, and Bayer are compared.
   Candidate scoring uses edge agreement, local tonal fidelity, ink density,
   connected-component noise, and page-type priors. Pages that are effectively
   bilevel (under 12% midtones) never dither. Only the winner is rendered
   over the full page.
10. If binarization leaves blank columns at the sides, the page is re-cropped
    horizontally and re-rendered so content spans the full printable width.
11. Text/logo/photo regions are re-binarized with a dedicated per-region
    pipeline (per-ROI threshold choice for text, Otsu for logos,
    Floyd–Steinberg for photos) and composited over the base result.
12. Machine-readable symbols are pasted last through a protected path (below).
13. Empty trailing rows are removed, the page is kept at exact byte-aligned
    printer width, and rows are packed for BLE.

## Protected QR/barcode path

QR codes and barcodes are never dithered, sharpened, CLAHE-enhanced or
gamma-corrected, and no geometry-altering filter touches them:

- Symbols decoded at detection time (this covers vector-drawn symbols, which
  rasterize cleanly at 600 DPI) are **regenerated from their payload**:
  `qrcode` rebuilds QR matrices, `python-barcode` rebuilds Code128/EAN/UPC/
  Code39/ITF/Codabar bar patterns. Modules are scaled to an integer number of
  printer dots (nearest-neighbor expansion, at least
  `symbol_min_module_dots` per module) with an enforced quiet zone
  (`symbol_quiet_modules`, default 4 for QR, 10 narrow modules for 1-D).
- Undecodable symbols are **re-rasterized from the PDF at
  `symbol_fallback_dpi` (default 1200)**, thresholded with plain Otsu on the
  unenhanced grayscale, and resized with nearest-neighbor only.
- The target area is cleared to white before pasting so the quiet zone is
  genuinely quiet, then the symbol is merged back into the final page.

Every region and the action taken (`regenerated`, `rethresholded`,
`region-threshold:...`, `region-dither`) is listed in the page report.

The dither comparison uses a deterministic width-preserving sample from long
pages. This still compares every configured algorithm but avoids spending
minutes rendering full-size candidates that will be discarded.

## Quality report

The document and page reports include:

- content, text, and image coverage percentages
- sharpness, readability, edge density, dynamic range, and page type
- content crop box and source geometry
- selected orientation and all orientation scores
- selected gamma and all gamma scores
- selected threshold/dither and every candidate score
- scaling factor, final dimensions, packed byte count, and trimmed paper rows

These are image heuristics, not OCR confidence. In particular, choosing between
0° and 180° for a scanned page is impossible reliably without OCR; equal-scale
ties preserve the PDF's source direction rather than guessing.

## Configuration

All controls live in `rendering.yaml`. Important options:

- `printer_width`: exact printable dot width; must be a multiple of 8
- `render_dpi`: 300–600
- `rotation_mode`: `auto`, `0`, `90`, `180`, or `270`
- `threshold_algorithm` / `dither_algorithm`: `auto` or a named candidate
- `crop_margin`, `content_safety_margin`, `white_threshold`
- `paper_trim`, `minimum_page_rows`, `save_intermediates`
- enhancement gamma, contrast, sharpness, CLAHE, denoise, and cleanup controls

The API and Bluetooth client read this same width configuration; 384 is a
default in YAML, not an assumption embedded in the renderer.
