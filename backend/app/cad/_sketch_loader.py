"""Shared sketch-loading helper used by every worker that needs to inject
the `sketches` dict into an object script — _script_worker (run + GLB),
_snapshot_worker (PNG render), and _scene_worker (multi-object scene).

Each helper runs in a clean subprocess; importing this module is cheap and
none of these helpers touch global state.
"""
from __future__ import annotations

import json
import runpy
from pathlib import Path


def build_workplane_from_plane(cq, plane):
    """Translate the user-friendly `plane` value from a sketch script into a
    cq.Workplane the sketch can be placed on."""
    if plane is None:
        return cq.Workplane("XY")
    if isinstance(plane, str):
        return cq.Workplane(plane)
    if isinstance(plane, tuple) and len(plane) == 2 and isinstance(plane[0], str):
        return cq.Workplane(plane[0]).workplane(offset=float(plane[1]))
    return cq.Workplane(plane)


def load_sketches_from_manifest(manifest_path: Path | None) -> dict:
    """Run each sketch script in the manifest and return a {name: cq.Workplane}
    dict with the sketch already placed on its plane.

    A sketch that fails to load (missing file, no `sketch` defined, bad
    plane spec) is silently skipped — a malformed sketch shouldn't kill
    the object run that consumes it.
    """
    if manifest_path is None or not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(manifest, list) or not manifest:
        return {}

    import cadquery as cq
    out: dict = {}
    for entry in manifest:
        name = entry.get("name")
        script_path = Path(entry.get("script", ""))
        params_path = Path(entry.get("params", ""))
        if not name or not script_path.exists():
            continue
        params: dict = {}
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                params = {}
        try:
            globs = runpy.run_path(str(script_path), init_globals={"params": params})
        except Exception:
            continue
        sketch = globs.get("sketch")
        if sketch is None:
            continue
        plane = globs.get("plane", "XY")
        try:
            wp = build_workplane_from_plane(cq, plane).placeSketch(sketch)
        except Exception:
            continue
        out[name] = wp
    return out
