"""YAML-backed configuration for the PDF rendering pipeline."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EnhancementConfig:
    auto_contrast: bool = True
    clahe: bool = True
    clahe_clip_limit: float = 2.0
    clahe_grid_size: int = 8
    gamma: str | float = "auto"
    gamma_candidates: list[float] = field(
        default_factory=lambda: [0.75, 0.9, 1.0, 1.1, 1.25]
    )
    contrast: float = 1.0
    sharpness: float = 1.0
    denoise_strength: int = 3
    morphological_cleanup: bool = True


@dataclass
class PipelineConfig:
    printer_width: int = 384
    printer_dpi: int = 203
    render_dpi: int = 450
    crop_margin: int = 4
    white_threshold: int = 248
    content_safety_margin: int = 2
    threshold_algorithm: str = "auto"
    dither_algorithm: str = "auto"
    rotation_mode: str = "auto"
    paper_trim: bool = True
    save_intermediates: bool = True
    cache_dir: str = ".render_cache"
    output_dir: str = "render_output"
    minimum_page_rows: int = 1
    # Region-aware processing: QR/barcodes are detected before enhancement and
    # rendered through a geometry-preserving path (no CLAHE/gamma/sharpen/dither).
    regions_enabled: bool = True
    symbol_min_module_dots: int = 2
    symbol_quiet_modules: int = 4
    symbol_fallback_dpi: int = 1200
    threshold_candidates: list[str] = field(
        default_factory=lambda: ["otsu", "adaptive", "sauvola", "niblack", "wolf"]
    )
    dither_candidates: list[str] = field(
        default_factory=lambda: [
            "none",
            "floyd_steinberg",
            "jarvis",
            "stucki",
            "atkinson",
            "bayer",
        ]
    )
    enhancement: EnhancementConfig = field(default_factory=EnhancementConfig)

    def validate(self) -> None:
        if self.printer_width <= 0 or self.printer_width % 8:
            raise ValueError("printer_width must be positive and byte-aligned (multiple of 8)")
        if not 300 <= self.render_dpi <= 600:
            raise ValueError("render_dpi must be between 300 and 600")
        if self.rotation_mode not in {"auto", "0", "90", "180", "270"}:
            raise ValueError("rotation_mode must be auto, 0, 90, 180, or 270")
        if self.threshold_algorithm != "auto" and (
            self.threshold_algorithm not in self.threshold_candidates
        ):
            raise ValueError("unsupported threshold_algorithm")
        if self.dither_algorithm != "auto" and (
            self.dither_algorithm not in self.dither_candidates
        ):
            raise ValueError("unsupported dither_algorithm")

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        raw: dict[str, Any] = {}
        with Path(path).open("r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream) or {}
        enhancement = EnhancementConfig(**raw.pop("enhancement", {}))
        config = cls(**raw, enhancement=enhancement)
        config.validate()
        return config

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
