"""Subprocess entrypoint: load a model file (STEP / IGES / BREP / STL)
and tessellate it to GLB.

VTK / OCCT operations live in a clean subprocess for the same reason
sketch + script workers do — a malformed input can hang or crash the
host on Windows.

Argv:
    1 = source_path     (path to .step / .stp / .iges / .igs / .brep / .brp / .stl)
    2 = output glb path
    3 = output json path  (always written; ok=True/False)
    4 = tessellation deflection (float)
"""
from __future__ import annotations

import json
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
    source_path = Path(sys.argv[1])
    glb_out = Path(sys.argv[2])
    json_out = Path(sys.argv[3])
    deflection = float(sys.argv[4]) if len(sys.argv) > 4 else 0.1

    result: dict = {"ok": False, "error": None, "meta": None}
    try:
        if not source_path.exists():
            result["error"] = f"import source not found: {source_path}"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        from app.cad._import_loader import load_to_workplane
        model = load_to_workplane(source_path)
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
