"""Slice presets exposed to the user.

Three presets at any time — strong / standard / fine. The mapping to
slicer-specific config (Bambu Studio JSON profile names, layer heights,
infill, etc.) lives in each slicer driver, not here. This file only
defines the abstract preset identity + a short human description so the
print pane and agent prompt can refer to them by id.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str


PRESETS: list[Preset] = [
    Preset(
        id="strong",
        label="Strong",
        description=(
            "Thicker layers, higher infill, solid walls — slower print, "
            "tougher mechanical part."
        ),
    ),
    Preset(
        id="standard",
        label="Standard",
        description="Balanced quality / time / strength. Reasonable default.",
    ),
    Preset(
        id="fine",
        label="Fine",
        description=(
            "Thin layers, gentler speeds — slowest print, cleanest "
            "surface and small features."
        ),
    ),
]


PRESET_IDS = [p.id for p in PRESETS]
DEFAULT_PRESET = "standard"


def lookup(preset_id: str) -> Preset:
    for p in PRESETS:
        if p.id == preset_id:
            return p
    raise KeyError(f"unknown preset {preset_id!r} (have: {PRESET_IDS})")
