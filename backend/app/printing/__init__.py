"""Print-phase machinery: slicers, printers, presets, per-project state.

The print phase is a separate UI mode that takes over the viewer area
while keeping the agent + chat available. The pipeline is:

    visible objects -> 3MF/STL export -> slicer (orient + slice with
    chosen preset + agent overrides) -> printer driver -> physical print

`Slicer` and `Printer` are ABCs so other vendors can be added later.
The current concrete implementations are:
    - BambuStudioSlicer  (Bambu Studio CLI, presets: strong/standard/fine)
    - BambuLabPrinter    (Bambu X1/X1C/P1/A1 in developer / LAN mode)
"""
from __future__ import annotations

from .presets import PRESET_IDS, PRESETS, Preset
from .printers import (
    PRINTER_KINDS,
    BambuLabPrinter,
    Printer,
    PrinterStatus,
    build_printer,
)
from .slicers import (
    SLICER_KINDS,
    BambuStudioSlicer,
    SliceOverride,
    Slicer,
    SliceResult,
    build_slicer,
)
from .state import PhaseState, PrintSession

__all__ = [
    "PRESET_IDS",
    "PRESETS",
    "Preset",
    "PRINTER_KINDS",
    "Printer",
    "PrinterStatus",
    "BambuLabPrinter",
    "build_printer",
    "SLICER_KINDS",
    "Slicer",
    "SliceOverride",
    "SliceResult",
    "BambuStudioSlicer",
    "build_slicer",
    "PhaseState",
    "PrintSession",
]
