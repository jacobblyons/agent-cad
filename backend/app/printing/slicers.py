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


# Map our preset ids to Bambu Studio's *shipped* process profile names.
# Bambu's CLI doesn't accept inline `--<setting> <value>` overrides;
# the only way in is a JSON profile passed via `--load-settings`. So
# each preset points at a profile that already lives on disk under
# `resources/profiles/BBL/process/`. We pick X1C 0.4mm-nozzle profiles
# by default — the user's printer config can override per machine.
#
# These names are version-stable: Bambu Studio has shipped Standard /
# Strength / Fine names for years and Bambu's own UI relies on them
# remaining unchanged.
_BAMBU_PRESET_PROCESS_NAMES: dict[str, str] = {
    "standard": "0.20mm Standard @BBL X1C",
    "strong":   "0.20mm Strength @BBL X1C",
    "fine":     "0.12mm Fine @BBL X1C",
}

# Default machine + filament profiles for the X1C 0.4mm nozzle —
# applied when the user hasn't pinned specific profile names in
# Settings. Both ship with every Bambu Studio install.
_BAMBU_DEFAULT_MACHINE_NAME = "Bambu Lab X1 Carbon 0.4 nozzle"
_BAMBU_DEFAULT_FILAMENT_NAME = "Bambu PLA Basic @BBL X1C"


# Map a Bambu material code (from the printer's MQTT report — `tray_type`
# or `tray_info_idx`) to a filament profile name suffix. We append the
# printer's profile (e.g. "@BBL X1C") at lookup time, so the same map
# works for any compatible machine.
#
# Bambu's `tray_info_idx` is the most reliable identifier — it's a stable
# code per filament SKU. The first 3 letters say the type ("GFA" = PLA,
# "GFG" = PETG, "GFB" = ABS, ...) and the rest is the brand/variant.
_BAMBU_TRAY_IDX_PREFIX_TO_FAMILY: dict[str, str] = {
    "GFA": "PLA",
    "GFB": "ABS",
    "GFC": "ASA",
    "GFG": "PETG",
    "GFL": "PLA",   # PLA-CF / specialty PLAs
    "GFN": "PA",    # Nylon
    "GFP": "PC",
    "GFS": "PVA",
    "GFT": "TPU",
    "GFU": "PET-CF",
}


# Specific tray_info_idx → exact filament profile name. Used when we
# can pin the precise match (eg. Bambu Basic vs. Matte vs. Silk). Falls
# back to a generic of-family name when the idx isn't here.
_BAMBU_TRAY_IDX_EXACT: dict[str, str] = {
    "GFA00": "Bambu PLA Basic",
    "GFA01": "Bambu PLA Matte",
    "GFA02": "Bambu PLA Silk",
    "GFA03": "Bambu PLA Tough",
    "GFA04": "Bambu PLA Aero",
    "GFA05": "Bambu PLA Galaxy",
    "GFA08": "Bambu PLA Marble",
    "GFA11": "Bambu PLA Metal",
    "GFA12": "Bambu PLA Glow",
    "GFA15": "Bambu PLA Dynamic",
    "GFA50": "Bambu PLA-CF",
    "GFB00": "Bambu ABS",
    "GFB50": "Bambu ABS-GF",
    "GFC00": "Bambu ASA",
    "GFC50": "Bambu ASA-CF",
    "GFG00": "Bambu PETG HF",
    "GFG01": "Bambu PETG Translucent",
    "GFG02": "Bambu PETG Basic",
    "GFG50": "Bambu PETG-CF",
    "GFP00": "Bambu PC",
    "GFP01": "Bambu PC FR",
    "GFN03": "Bambu PA-CF",
    "GFN04": "Bambu PAHT-CF",
    "GFN05": "Bambu PA6-CF",
    "GFN08": "Bambu PA6-GF",
    "GFU00": "Bambu PET-CF",
    # GFG99 / unknowns fall back to the per-family default below.
}


# Per-material-family fallback profile when `tray_info_idx` is unknown
# (e.g. third-party spool reported as "GFG99"). These are the actual
# profile names Bambu ships for the X1C — verified by `ls` against
# `resources/profiles/BBL/filament/`. There aren't "Generic <material>"
# profiles for the common families (PLA / PETG / ABS / ASA / PC), so we
# fall back to the matching Bambu-branded base profile, which is the
# closest analogue at sane temps.
_BAMBU_FAMILY_FALLBACK: dict[str, str] = {
    "PLA":   "Bambu PLA Basic",
    "ABS":   "Bambu ABS",
    "ASA":   "Bambu ASA",
    "PETG":  "Bambu PETG Basic",
    "PC":    "Bambu PC",
    "PA":    "Bambu PA-CF",
    "TPU":   "Generic TPU for AMS",   # the only generic TPU Bambu ships
    "PET-CF": "Bambu PET-CF",
}


def _filament_suffix_for_machine(machine_name: str) -> str:
    """Translate a machine profile name like
    "Bambu Lab X1 Carbon 0.4 nozzle" into the filament-naming suffix
    Bambu uses: "@BBL X1C". Falls back to "@BBL X1C" when we can't
    parse the machine name.
    """
    n = (machine_name or "").lower()
    if "x1 carbon" in n or "x1c" in n:
        return "@BBL X1C"
    if "x1e" in n:
        return "@BBL X1E"
    if "p1s" in n:
        return "@BBL P1S"
    if "p1p" in n:
        return "@BBL P1P"
    if "a1 mini" in n:
        return "@BBL A1M"
    if "a1" in n:
        return "@BBL A1"
    return "@BBL X1C"


def _filament_name_for_slot(
    *,
    tray_type: str,
    tray_info_idx: str,
    machine_suffix: str,
) -> str:
    """Pick the closest Bambu-shipped filament profile name for a slot
    the printer reported. Returns just the profile name (no `.json`).
    Slicer's `_resolve_profile` does the existence check — when the
    chosen name doesn't exist on disk the slicer falls back to its
    configured default."""
    suffix = machine_suffix or "@BBL X1C"
    # 1. Exact tray_info_idx match wins (most accurate).
    base = _BAMBU_TRAY_IDX_EXACT.get(tray_info_idx)
    if base:
        return f"{base} {suffix}"
    # 2. Family by tray_info_idx prefix.
    if tray_info_idx and len(tray_info_idx) >= 3:
        family = _BAMBU_TRAY_IDX_PREFIX_TO_FAMILY.get(tray_info_idx[:3])
        if family:
            base = _BAMBU_FAMILY_FALLBACK.get(family)
            if base:
                return f"{base} {suffix}"
    # 3. Family by tray_type (PLA / PETG / ABS / ...).
    fam = (tray_type or "").upper()
    base = _BAMBU_FAMILY_FALLBACK.get(fam)
    if base:
        return f"{base} {suffix}"
    # 4. Last resort — Bambu PLA Basic. Slicer will surface a clear
    # error if even this is missing.
    return f"Bambu PLA Basic {suffix}"


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

    def _profile_root(self, cli_path: str) -> Path | None:
        """Walk up from the CLI binary to find the BBL profiles directory.

        Bambu Studio installs the slicer under
        `<install>/bambu-studio.exe` and the system profiles under
        `<install>/resources/profiles/BBL/`. We try a couple of layouts so
        the discovery still works on Mac (`.app` bundle) and Linux."""
        cli = Path(cli_path).resolve()
        # Windows / Linux layout: cli sits in <install>/, profiles under <install>/resources/profiles/BBL/
        for parent in (cli.parent, cli.parent.parent):
            cand = parent / "resources" / "profiles" / "BBL"
            if cand.is_dir():
                return cand
        # Mac .app bundle: BambuStudio.app/Contents/MacOS/BambuStudio,
        # profiles under .app/Contents/Resources/profiles/BBL/.
        for ancestor in cli.parents:
            cand = ancestor / "Resources" / "profiles" / "BBL"
            if cand.is_dir():
                return cand
        return None

    def _resolve_profile(self, root: Path, kind: str, name: str) -> Path | None:
        """Find a profile JSON by name under <root>/<kind>/. Returns None
        when missing — callers fall back to defaults."""
        if not name:
            return None
        path = root / kind / f"{name}.json"
        return path if path.exists() else None

    def _resolve_inheritance_chain(
        self, root: Path, kind: str, name: str,
    ) -> list[Path]:
        """Walk the profile's `inherits` field up to the root and return
        the chain in parent→child order. Empty list if the leaf doesn't
        exist."""
        chain: list[Path] = []
        seen: set[str] = set()
        current = name
        while current and current not in seen:
            seen.add(current)
            path = root / kind / f"{current}.json"
            if not path.exists():
                break
            chain.append(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
            current = (data.get("inherits") or "").strip()
        chain.reverse()
        return chain

    def _flatten_profile(
        self, root: Path, kind: str, name: str, out_dir: Path,
    ) -> Path | None:
        """Resolve `name` plus its `inherits` chain into a single flat
        profile JSON written to `out_dir`. Returns the temp file path
        or None if the leaf can't be found.

        Why we flatten ourselves rather than passing the chain: Bambu
        Studio's CLI doesn't traverse `inherits` for `machine` or
        `filament` kinds — anything defined only on a parent (the X1C's
        `machine_end_gcode`, all the temps on a non-PLA filament) goes
        missing. And for filaments, `--load-filaments a.json;b.json`
        means "two filament slots", not inheritance. We do the merge by
        deep-copying the root parent and then layering each child on top
        in parent→child order. The result is a self-contained leaf with
        no `inherits` field, which the slicer can ingest unambiguously.
        """
        chain = self._resolve_inheritance_chain(root, kind, name)
        if not chain:
            return None
        merged: dict = {}
        for p in chain:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for k, v in data.items():
                # `inherits` is meaningless on a flattened profile — drop
                # it so Bambu doesn't try (and fail) to chase the chain
                # again from the temp file's location.
                if k == "inherits":
                    continue
                merged[k] = v
        merged["from"] = "system"
        # Keep the leaf's name so `compatible_printers` matching still
        # works against the printer profile we're loading.
        merged["name"] = chain[-1].stem
        suffix = uuid.uuid4().hex[:6]
        out_path = out_dir / f"{kind}-flat-{suffix}.json"
        out_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        return out_path

    def _apply_overrides_to_process(
        self,
        base_process_path: Path,
        overrides: list[SliceOverride],
        bed_type: str,
        out_dir: Path,
    ) -> Path:
        """Generate a process profile JSON that derives from the system
        profile and adds the user/agent overrides + the build plate.

        Bambu Studio's CLI accepts settings only through profile JSONs
        (no inline `--<key> <value>` flags), so anything we want to
        change at slice time has to land on the JSON. Two sources of
        change feed in here:

          1. Agent / user overrides (key→value, free-form).
          2. The current build plate (`curr_bed_type`) — must be set
             explicitly because the X1C's MQTT report doesn't carry it
             on current firmware, so we either get it from the
             auto-detected printer state or from the printer's
             `default_bed_type` config field.

        We keep the system machine profile untouched — all changes are
        process-side. `compatible_printers` stays as-is so the system
        machine still matches.
        """
        try:
            process = json.loads(base_process_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            process = {}

        if bed_type:
            # `curr_bed_type` is what Bambu Studio's GUI sets when the
            # user picks a plate. The slicer reads this to look up
            # initial-layer bed temp from the filament profile.
            process["curr_bed_type"] = bed_type

        for ov in overrides:
            key = _BAMBU_OVERRIDE_KEY_ALIASES.get(ov.key, ov.key)
            value = _coerce_bambu_value(key, ov.value)
            existing = process.get(key)
            # Bambu stores per-extruder settings as parallel arrays;
            # broadcast our single value across all entries to match.
            if isinstance(existing, list) and existing:
                process[key] = [value] * len(existing)
            else:
                process[key] = value
        # Unique name so Bambu's profile registry treats this as a fresh
        # entry rather than trying to merge with the system profile.
        suffix = uuid.uuid4().hex[:6]
        process["name"] = f"{process.get('name', base_process_path.stem)} (override-{suffix})"
        out_path = out_dir / f"process-override-{suffix}.json"
        out_path.write_text(json.dumps(process, indent=2), encoding="utf-8")
        return out_path

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

        profile_root = self._profile_root(cli)
        if profile_root is None:
            return SliceResult(ok=False, error=(
                "found Bambu Studio CLI but couldn't locate its profile "
                "directory (resources/profiles/BBL/). Install may be corrupt."
            ))

        # Resolve machine / process / filament profiles.
        # Each can be overridden via the printer config or the slicer's
        # own config; otherwise we fall back to X1C-0.4mm + Bambu PLA
        # Basic + the per-preset process choice.
        hint = printer_hint or {}
        machine_name = (
            hint.get("printer_profile")
            or self.config.printer_profile
            or _BAMBU_DEFAULT_MACHINE_NAME
        )
        process_name = (
            hint.get("process_profile")
            or self.config.process_profile
            or _BAMBU_PRESET_PROCESS_NAMES.get(preset, _BAMBU_PRESET_PROCESS_NAMES["standard"])
        )
        # Auto-detected filament info wins over a hard-coded
        # filament_profile setting. The printer's MQTT report tells us
        # exactly what's loaded (PLA / PETG / ABS / ...) so we map that
        # to the matching system profile name. Falls through to the
        # hard-coded filament_profile / default when no detection ran.
        detected_tray_type = str(hint.get("detected_tray_type") or "").strip()
        detected_tray_idx = str(hint.get("detected_tray_info_idx") or "").strip()
        if detected_tray_type or detected_tray_idx:
            # Strip "Bambu Lab " prefix off the machine name to get the
            # filament profile suffix. "Bambu Lab X1 Carbon 0.4 nozzle"
            # -> "@BBL X1C". This matches Bambu's profile naming.
            suffix = _filament_suffix_for_machine(machine_name)
            filament_name = _filament_name_for_slot(
                tray_type=detected_tray_type,
                tray_info_idx=detected_tray_idx,
                machine_suffix=suffix,
            )
        else:
            filament_name = (
                hint.get("filament_profile")
                or self.config.filament_profile
                or _BAMBU_DEFAULT_FILAMENT_NAME
            )

        # Build plate — auto-detected when MQTT reported it, otherwise
        # the printer config's user-set fallback.
        bed_type = str(
            hint.get("detected_bed_type_slicer")
            or hint.get("default_bed_type")
            or ""
        ).strip()

        machine_path = self._resolve_profile(profile_root, "machine", machine_name)
        process_path = self._resolve_profile(profile_root, "process", process_name)
        # Machine + filament: leaf-only loads drop everything defined on
        # parent profiles because Bambu's CLI doesn't traverse `inherits`
        # for these kinds. The X1C leaf has `machine_end_gcode: None` —
        # the real end sequence (turn off bed, park, lower z) lives in
        # `fdm_bbl_3dp_001_common`. Same shape for non-PLA filaments
        # (temps, fan curves, etc. are on parents). We flatten both into
        # self-contained temp files. Process inheritance is fine — Bambu
        # CLI handles it for that kind.
        filament_leaf_path = self._resolve_profile(profile_root, "filament", filament_name)

        missing: list[str] = []
        if machine_path is None:
            missing.append(f"machine {machine_name!r}")
        if process_path is None:
            missing.append(f"process {process_name!r}")
        if filament_leaf_path is None:
            missing.append(f"filament {filament_name!r}")
        if missing:
            return SliceResult(ok=False, error=(
                f"could not find Bambu Studio profile(s): {', '.join(missing)}. "
                f"Looked in {profile_root}. Confirm Bambu Studio is fully "
                "installed; if you customised profile names in Settings, "
                "make sure they match what's on disk."
            ))

        out_dir.mkdir(parents=True, exist_ok=True)
        # Bambu's CLI emits sliced output as a `.gcode.3mf` file (a 3MF
        # archive carrying the gcode plus plate metadata). Match that
        # naming so the printer's "Print Now" / our LAN driver recognise
        # it as a sliced job.
        out_3mf = out_dir / f"sliced-{uuid.uuid4().hex[:8]}.gcode.3mf"

        # Process profile is regenerated whenever we have overrides OR
        # need to set the build plate. Without overrides AND without a
        # bed_type override we point Bambu directly at the system file
        # — that's the documented happy path and avoids any merge tricks.
        effective_process_path = process_path
        if overrides or bed_type:
            effective_process_path = self._apply_overrides_to_process(
                process_path, overrides, bed_type, out_dir,
            )

        # The documented one-shot invocation per Bambu's wiki:
        #   bambu-studio --orient 1 --arrange 1 --slice 1 \
        #                --load-settings "machine.json;process.json" \
        #                --load-filaments "filament.json" \
        #                --allow-newer-file \
        #                --export-3mf out.gcode.3mf \
        #                model.stl
        #
        # --orient 1 picks the printable side as "down" so overhangs
        # don't tip the part into a print failure (CADQuery emits
        # whatever orientation the script wrote, which is rarely the
        # best for an FDM printer). --arrange 1 places the oriented
        # part on the bed. --slice 1 then slices plate 1 (the first
        # and, for our single-object exports, only plate).
        # --allow-newer-file lets Bambu accept 3MFs whose minor version
        # is newer than the slicer's parser expects — CADQuery's
        # exporter sometimes ships a slightly newer revision and this
        # avoids a hard reject.
        # See: https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
        # Always pass flattened single-file machine + filament profiles —
        # bypasses Bambu CLI's broken inheritance resolution. For
        # filaments it also bypasses the multi-slot interpretation of
        # multi-path --load-filaments. For machines, this is what makes
        # the proper end-gcode (M140 S0, park-to-back, lower-bed) reach
        # the slice instead of the empty-string default on the leaf.
        flat_machine = self._flatten_profile(
            profile_root, "machine", machine_name, out_dir,
        )
        if flat_machine is None:
            return SliceResult(ok=False, error=(
                f"failed to flatten machine profile {machine_name!r}"
            ))
        flat_filament = self._flatten_profile(
            profile_root, "filament", filament_name, out_dir,
        )
        if flat_filament is None:
            return SliceResult(ok=False, error=(
                f"failed to flatten filament profile {filament_name!r}"
            ))
        argv = [
            cli,
            "--allow-newer-file",
            "--load-settings", f"{flat_machine};{effective_process_path}",
            "--load-filaments", str(flat_filament),
            "--orient", "1",
            "--arrange", "1",
            "--slice", "1",
            "--export-3mf", str(out_3mf),
        ]
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
            # Friendly handling for the well-known v2.04 / v2.05 CLI bug
            # (https://github.com/bambulab/BambuStudio/issues/9636) — a
            # single-extruder slice errors out with `nozzle_volume_type
            # not found` and segfaults. Surface a clear next step rather
            # than a raw 0xC0000005 dump.
            if "nozzle_volume_type not found" in log:
                return SliceResult(
                    ok=False,
                    error=(
                        "Bambu Studio CLI v2.04+ has a known bug where "
                        "single-extruder slicing crashes with "
                        "'nozzle_volume_type not found' "
                        "(github.com/bambulab/BambuStudio/issues/9636). "
                        "The slice itself is fine; the CLI is the problem. "
                        "Workarounds: install OrcaSlicer (a community fork "
                        "that fixed this), or downgrade Bambu Studio to a "
                        "pre-2.04 release. Either way, re-point "
                        "Settings -> Printers -> CLI path at the working "
                        "binary."
                    ),
                    log=log,
                )
            return SliceResult(
                ok=False,
                error=(
                    f"Bambu Studio CLI slice failed (exit {proc.returncode}). "
                    f"Last 600 chars of log:\n{log[-600:]}"
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
