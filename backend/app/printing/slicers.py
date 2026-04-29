"""Slicer drivers — turn a 3D model file into printer-ready instructions.

A `Slicer` takes one or more model files (STL / 3MF / STEP — vendor
dependent), a preset id, optional per-key overrides, and returns a
`SliceResult` describing the produced sliced job (path on disk, time
estimate, filament estimate, oriented model preview if any).

Implementations live in this module. `BambuStudioSlicer` shells out to
the Bambu Studio CLI (`bambu-studio` / `BambuStudio.exe` / `BambuStudio
--no-gui` depending on platform). Other slicers (PrusaSlicer, Cura,
OrcaSlicer) can be added as separate classes implementing the same ABC.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .presets import DEFAULT_PRESET, PRESET_IDS

SLICER_KINDS = ["bambu_studio"]


@dataclass
class SliceOverride:
    """A user- or agent-supplied tweak that overrides one preset value.

    `key` is slicer-agnostic (e.g. "infill_density", "support",
    "layer_height") — each slicer translates to its own config field.
    `value` is stored as a string so we can pass through any literal the
    slicer accepts (number, percent, bool, enum). UI displays it raw.
    """
    key: str
    value: str
    note: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class SliceResult:
    ok: bool
    error: str | None = None
    sliced_path: str | None = None       # absolute path to the sliced file
    sliced_format: str | None = None     # "3mf" | "gcode"
    estimated_minutes: float | None = None
    estimated_filament_g: float | None = None
    estimated_filament_m: float | None = None
    plate_image_b64: str | None = None   # PNG preview of the plate, if available
    oriented_model_path: str | None = None  # the post-orient input file
    log: str = ""

    def to_json(self) -> dict:
        return asdict(self)


class Slicer(ABC):
    """Vendor-agnostic slicer interface.

    Each concrete slicer carries its own config (CLI path, profile
    library location, machine-specific quirks). Build via
    `build_slicer(kind, config)` rather than instantiating directly so
    new vendors plug in cleanly later.
    """
    kind: str

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Return (ok, reason). When ok=False, reason explains why
        slicing won't work — typically "Bambu Studio CLI not found"."""

    @abstractmethod
    def auto_orient_and_slice(
        self,
        model_paths: list[Path],
        *,
        preset: str,
        overrides: list[SliceOverride],
        out_dir: Path,
        printer_hint: dict | None = None,
    ) -> SliceResult:
        """Auto-orient `model_paths` then slice with `preset` + `overrides`.

        Output sliced file lives under `out_dir`. `printer_hint` lets the
        slicer pick the right printer/process/filament profiles for the
        physical machine that will print it.
        """


# --------------------------------------------------------------------- #
# Bambu Studio CLI driver                                                #
# --------------------------------------------------------------------- #


@dataclass
class BambuStudioConfig:
    """Config for the BambuStudioSlicer.

    `cli_path` lets the user override auto-discovery (the CLI binary's
    name and location varies between Bambu's installer, Flatpak, Mac
    bundle, and the OrcaSlicer fork). Empty = autodiscover.

    `printer_profile` / `process_profile` / `filament_profile` map to
    Bambu Studio's JSON profile names. They're optional — when blank we
    pass per-preset defaults below. When the user has configured a
    Bambu printer, we copy its profile name in here so each slice uses
    the right machine config (X1C 0.4mm nozzle, etc.).
    """
    cli_path: str = ""
    printer_profile: str = ""
    process_profile: str = ""
    filament_profile: str = ""

    def to_json(self) -> dict:
        return asdict(self)


# Per-preset Bambu Studio process tweaks. Bambu Studio has its own
# "Standard / Fine / Strong" process profile names that ship with the
# install, but they vary by version, so we set explicit values via
# CLI key=val overrides. Slicer-specific keys live here, behind the
# preset id which is what the rest of the app speaks.
_BAMBU_PRESET_OVERRIDES: dict[str, dict[str, str]] = {
    "strong": {
        "layer_height": "0.28",
        "first_layer_height": "0.3",
        "sparse_infill_density": "30%",
        "wall_loops": "4",
        "top_shell_layers": "5",
        "bottom_shell_layers": "4",
    },
    "standard": {
        "layer_height": "0.2",
        "first_layer_height": "0.2",
        "sparse_infill_density": "15%",
        "wall_loops": "3",
        "top_shell_layers": "4",
        "bottom_shell_layers": "3",
    },
    "fine": {
        "layer_height": "0.12",
        "first_layer_height": "0.16",
        "sparse_infill_density": "15%",
        "wall_loops": "3",
        "top_shell_layers": "5",
        "bottom_shell_layers": "4",
    },
}


# Map our generic override keys → Bambu Studio config keys. Anything
# the agent passes that isn't in this map is forwarded verbatim, so
# agents that already speak Bambu's vocabulary still work.
_BAMBU_OVERRIDE_KEY_ALIASES: dict[str, str] = {
    "infill_density": "sparse_infill_density",
    "infill": "sparse_infill_density",
    "walls": "wall_loops",
    "perimeters": "wall_loops",
    "layer_height": "layer_height",
    "support": "enable_support",
    "supports": "enable_support",
    "brim": "brim_type",
    "raft": "raft_layers",
    "skirts": "skirt_loops",
    "top_layers": "top_shell_layers",
    "bottom_layers": "bottom_shell_layers",
}


def _coerce_bambu_value(key: str, value: str) -> str:
    """Translate generic boolean values into Bambu's accepted form for
    flag-shaped settings. Pass non-bool values through unchanged."""
    bool_keys = {"enable_support", "enable_arc_fitting", "smooth_speed_discontinuity_area"}
    if key in bool_keys:
        v = value.strip().lower()
        if v in ("on", "true", "yes", "1", "enable", "enabled"):
            return "1"
        if v in ("off", "false", "no", "0", "disable", "disabled"):
            return "0"
    return value


class BambuStudioSlicer(Slicer):
    kind = "bambu_studio"

    def __init__(self, config: BambuStudioConfig | dict | None = None):
        if isinstance(config, dict):
            config = BambuStudioConfig(**config)
        self.config = config or BambuStudioConfig()

    # -------- discovery -------------------------------------------------

    def _discover_cli(self) -> str | None:
        """Look up the Bambu Studio executable on disk.

        Bambu ships the *same* binary as both the GUI and the slicer CLI
        — passing `--export-3mf` / `--slice` flags makes it run headless.
        We search in the order most likely to hit on the user's box.
        """
        if self.config.cli_path:
            p = Path(self.config.cli_path).expanduser()
            if p.exists():
                return str(p)
            # Allow plain executable names that are on PATH.
            found = shutil.which(self.config.cli_path)
            if found:
                return found
        candidates: list[str] = []
        if os.name == "nt":
            candidates = [
                r"C:\Program Files\Bambu Studio\bambu-studio.exe",
                r"C:\Program Files\Bambu Studio\BambuStudio.exe",
                r"C:\Program Files (x86)\Bambu Studio\bambu-studio.exe",
            ]
        else:
            candidates = [
                "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio",
                "/usr/bin/bambu-studio",
                "/usr/local/bin/bambu-studio",
            ]
        for c in candidates:
            if Path(c).exists():
                return c
        for name in ("bambu-studio", "BambuStudio", "bambu_studio"):
            found = shutil.which(name)
            if found:
                return found
        return None

    def is_available(self) -> tuple[bool, str]:
        cli = self._discover_cli()
        if not cli:
            return False, (
                "Bambu Studio CLI was not found. Install Bambu Studio "
                "(https://bambulab.com/en/download/studio) or set the path "
                "explicitly in Settings → Printers → CLI path."
            )
        return True, cli

    # -------- slice ----------------------------------------------------

    def auto_orient_and_slice(
        self,
        model_paths: list[Path],
        *,
        preset: str,
        overrides: list[SliceOverride],
        out_dir: Path,
        printer_hint: dict | None = None,
    ) -> SliceResult:
        if preset not in PRESET_IDS:
            return SliceResult(ok=False, error=f"unknown preset {preset!r}")
        if not model_paths:
            return SliceResult(ok=False, error="no model files to slice")

        ok, cli_or_reason = self.is_available()
        if not ok:
            return SliceResult(ok=False, error=cli_or_reason)
        cli = cli_or_reason

        out_dir.mkdir(parents=True, exist_ok=True)
        out_3mf = out_dir / f"sliced-{uuid.uuid4().hex[:8]}.3mf"

        # Build the CLI argv. Bambu Studio's CLI accepts a sequence of
        # mode flags + config overrides. The high-level flow:
        #
        #   <cli> --orient 1 --slice 0 --export-3mf <out.3mf>
        #         [--load-settings <printer.json>;<process.json>;<filament.json>]
        #         [--load-filaments <filament.json>]
        #         <model.stl> [<model2.stl>...]
        #
        # We start by exporting an oriented .3mf (so the agent / user can
        # see the orientation), then a second pass slices that 3mf in
        # place with the preset overrides.
        argv: list[str] = [cli]

        # Profiles. Caller can pin them through `printer_hint` (preferred,
        # so each Bambu printer config carries its own machine choice) or
        # via the slicer's own static config.
        printer_profile = (
            (printer_hint or {}).get("printer_profile")
            or self.config.printer_profile
        )
        process_profile = (
            (printer_hint or {}).get("process_profile")
            or self.config.process_profile
        )
        filament_profile = (
            (printer_hint or {}).get("filament_profile")
            or self.config.filament_profile
        )
        load_pieces = [p for p in (printer_profile, process_profile, filament_profile) if p]
        if load_pieces:
            argv.extend(["--load-settings", ";".join(load_pieces)])

        # Preset → key=value overrides → user/agent overrides (last wins).
        merged: dict[str, str] = {}
        merged.update(_BAMBU_PRESET_OVERRIDES.get(preset, {}))
        for ov in overrides:
            key = _BAMBU_OVERRIDE_KEY_ALIASES.get(ov.key, ov.key)
            merged[key] = _coerce_bambu_value(key, ov.value)
        for k, v in merged.items():
            argv.extend([f"--{k}", v])

        argv.extend(["--orient", "1", "--slice", "0"])
        argv.extend(["--export-3mf", str(out_3mf)])
        argv.extend(str(p) for p in model_paths)

        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=300,
            )
        except FileNotFoundError as e:
            return SliceResult(ok=False, error=f"could not launch Bambu Studio CLI: {e}")
        except subprocess.TimeoutExpired:
            return SliceResult(ok=False, error="Bambu Studio CLI timed out (>5 min)")

        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        if proc.returncode != 0 or not out_3mf.exists():
            return SliceResult(
                ok=False,
                error=(
                    f"Bambu Studio CLI failed (exit {proc.returncode}). "
                    f"Last 600 chars of log:\n{log[-600:]}"
                ),
                log=log,
            )

        # Second pass: actually slice the oriented 3mf in place.
        slice_argv: list[str] = [cli]
        if load_pieces:
            slice_argv.extend(["--load-settings", ";".join(load_pieces)])
        for k, v in merged.items():
            slice_argv.extend([f"--{k}", v])
        slice_argv.extend(["--slice", "0", str(out_3mf)])
        try:
            sproc = subprocess.run(
                slice_argv, capture_output=True, text=True, timeout=300,
            )
        except subprocess.TimeoutExpired:
            return SliceResult(
                ok=False,
                error="Bambu Studio CLI slice pass timed out (>5 min)",
                log=log,
            )
        slice_log = (sproc.stdout or "") + ("\n" + sproc.stderr if sproc.stderr else "")
        log += "\n--- slice pass ---\n" + slice_log
        if sproc.returncode != 0:
            return SliceResult(
                ok=False,
                error=(
                    f"Bambu Studio slice failed (exit {sproc.returncode}). "
                    f"Last 600 chars of log:\n{slice_log[-600:]}"
                ),
                log=log,
            )

        meta = _parse_bambu_slice_log(log)
        return SliceResult(
            ok=True,
            sliced_path=str(out_3mf),
            sliced_format="3mf",
            estimated_minutes=meta.get("minutes"),
            estimated_filament_g=meta.get("filament_g"),
            estimated_filament_m=meta.get("filament_m"),
            log=log,
        )


# Bambu Studio doesn't have a structured stdout — we have to parse the
# human-readable lines it prints. These regexes match the lines the
# current build emits; if Bambu changes the wording the parse silently
# returns None and the UI just shows "—" for the missing field.
_RE_TIME = re.compile(
    r"(?i)(?:print[\s_]*time|estimated[\s_]*time)\D+(\d+)\s*h(?:our)?s?[\s,]*(\d+)\s*m(?:in)?", re.IGNORECASE,
)
_RE_TIME_MIN = re.compile(r"(?i)(?:print[\s_]*time|estimated[\s_]*time)\D+(\d+)\s*m(?:in)?")
_RE_FILAMENT_G = re.compile(r"(?i)filament\D+(\d+(?:\.\d+)?)\s*g")
_RE_FILAMENT_M = re.compile(r"(?i)filament\D+(\d+(?:\.\d+)?)\s*m\b")


def _parse_bambu_slice_log(log: str) -> dict:
    """Extract whatever time / filament numbers Bambu Studio prints.

    Best-effort: missing fields stay None, the UI handles that. Run
    against the slice pass log so we don't pick up earlier orient-only
    output."""
    out: dict[str, float | None] = {"minutes": None, "filament_g": None, "filament_m": None}
    m = _RE_TIME.search(log)
    if m:
        try:
            out["minutes"] = float(int(m.group(1)) * 60 + int(m.group(2)))
        except ValueError:
            pass
    if out["minutes"] is None:
        m = _RE_TIME_MIN.search(log)
        if m:
            try:
                out["minutes"] = float(m.group(1))
            except ValueError:
                pass
    m = _RE_FILAMENT_G.search(log)
    if m:
        try:
            out["filament_g"] = float(m.group(1))
        except ValueError:
            pass
    m = _RE_FILAMENT_M.search(log)
    if m:
        try:
            out["filament_m"] = float(m.group(1))
        except ValueError:
            pass
    return out


def build_slicer(kind: str, config: dict | None = None) -> Slicer:
    """Factory — keeps the rest of the app from caring about which
    concrete class implements a given slicer kind."""
    if kind == "bambu_studio":
        return BambuStudioSlicer(config or {})
    raise ValueError(f"unknown slicer kind {kind!r} (have: {SLICER_KINDS})")
