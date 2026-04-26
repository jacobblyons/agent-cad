"""Subprocess entrypoint: load + run a project's script and write results.

Argv:
    1 = script_path     (path to the .py file defining `model`)
    2 = params_path     (path to params JSON; may be missing/empty)
    3 = output glb path
    4 = output json path  (always written; ok=True/False)
    5 = tessellation deflection (float)
    6 = sketches manifest path (optional; '-' = none)

Sketches manifest format (JSON):
    [
      {"name": "base", "script": "/abs/path/sketches/base.py",
                       "params": "/abs/path/sketches/base.params.json"},
      ...
    ]
Each sketch script is run before the object's script. Each must define
top-level `sketch` (a cq.Sketch) and may define `plane` (a string like
"XY", a (str, float) offset tuple, or a full cq.Plane). The runtime
builds `cq.Workplane(plane).placeSketch(sketch)` and accumulates these
into a `sketches` dict that's injected into the object script alongside
its own `params`.
"""
from __future__ import annotations

import json
import runpy
import sys
import traceback
from pathlib import Path


def _meta_for_workplane(model) -> dict:
    shape = model.val() if hasattr(model, "val") and callable(model.val) else model
    bb = shape.BoundingBox()
    meta = {
        "bbox": {
            "min": [bb.xmin, bb.ymin, bb.zmin],
            "max": [bb.xmax, bb.ymax, bb.zmax],
            "size": [bb.xlen, bb.ylen, bb.zlen],
            "diagonal": bb.DiagonalLength,
        },
        "face_count": len(shape.Faces()),
        "edge_count": len(shape.Edges()),
        "vertex_count": len(shape.Vertices()),
    }
    try:
        meta["volume"] = shape.Volume()
    except Exception:
        meta["volume"] = None
    try:
        meta["area"] = shape.Area()
    except Exception:
        meta["area"] = None
    return meta


def main() -> int:
    script_path = Path(sys.argv[1])
    params_path = Path(sys.argv[2])
    glb_out = Path(sys.argv[3])
    json_out = Path(sys.argv[4])
    deflection = float(sys.argv[5])
    sketches_manifest = (
        Path(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] != "-" else None
    )

    result: dict = {"ok": False, "error": None, "meta": None}
    try:
        params: dict = {}
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError as e:
                result["error"] = f"params file is invalid: {e}"
                json_out.write_text(json.dumps(result), encoding="utf-8")
                return 0

        if not script_path.exists():
            result["error"] = f"script not found: {script_path}"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        from app.cad._sketch_loader import load_sketches_from_manifest
        sketches = load_sketches_from_manifest(sketches_manifest)

        globs = runpy.run_path(
            str(script_path),
            init_globals={"params": params, "sketches": sketches},
        )

        model = globs.get("model")
        if model is None:
            result["error"] = f"{script_path.name} finished without defining `model`"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        result["meta"] = _meta_for_workplane(model)

        from app.cad.tessellate import to_glb, topology
        glb_out.write_bytes(to_glb(model, deflection=deflection))
        result["topology"] = topology(model)
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()

    json_out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
