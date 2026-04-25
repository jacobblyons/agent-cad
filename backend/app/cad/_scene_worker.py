"""Subprocess: load N CADQuery scripts, optionally combine them, render a PNG.

Used by the agent's scene_snapshot / section_snapshot / preview_boolean
tools. Same subprocess-isolation rationale as the single-object snapshot
worker: VTK isn't reliably thread-safe on Windows.

The scene spec is passed as a JSON file path (rather than argv) because
multi-object specs can get long.

Argv:
    1 = scene spec JSON path  (consumed)
    2 = output PNG path
    3 = output JSON path  (always written; ok=True/False)

Scene spec shape:
{
  "items": [
    {"name": "main", "script": "/abs/path/main.py", "params": "/abs/path/main.params.json"},
    ...
  ],
  "post": null  # render every item directly, OR
        | {"kind": "section",  "axis": "X|Y|Z", "offset": 0.0, "side": "above|below"}
        | {"kind": "boolean",  "op": "union|intersection|difference", "a": "main", "b": "lid"},
  "view":   {"preset": "iso"} | {"position": [...], "target": [...], "up": [...]},
  "width":  900,
  "height": 700
}
"""
from __future__ import annotations

import json
import runpy
import sys
import traceback
from pathlib import Path


def _load_item(item: dict) -> dict:
    """Run the script and pull out `model`. Returns {name, model}."""
    script = Path(item["script"])
    params_path = Path(item["params"])
    if not script.exists():
        raise FileNotFoundError(f"script not found: {script}")
    params: dict = {}
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            raise RuntimeError(f"params file is invalid: {e}") from e
    globs = runpy.run_path(str(script), init_globals={"params": params})
    model = globs.get("model")
    if model is None:
        raise RuntimeError(f"{script.name} finished without defining `model`")
    return {"name": item.get("name") or script.stem, "model": model}


def _section(workplane, axis: str, offset: float, side: str):
    """Cut the workplane with a half-space; keep the requested side."""
    import cadquery as cq

    shape = workplane.val() if hasattr(workplane, "val") and callable(workplane.val) else workplane
    bb = shape.BoundingBox()
    diag = max(bb.DiagonalLength, 1.0) * 5.0
    cx = 0.5 * (bb.xmin + bb.xmax)
    cy = 0.5 * (bb.ymin + bb.ymax)
    cz = 0.5 * (bb.zmin + bb.zmax)

    axis = axis.upper()
    side = side.lower()
    # `centered` arg controls which face of the box sits on the workplane.
    # For section purposes we want the cutter to occupy the "removed" half.
    if axis == "Z":
        if side == "above":
            cutter = cq.Workplane().box(diag, diag, diag, centered=(True, True, False))
            cutter = cutter.translate((cx, cy, offset))
        else:  # below
            cutter = cq.Workplane().box(diag, diag, diag, centered=(True, True, False))
            cutter = cutter.translate((cx, cy, offset - diag))
    elif axis == "X":
        if side == "above":
            cutter = cq.Workplane().box(diag, diag, diag, centered=(False, True, True))
            cutter = cutter.translate((offset, cy, cz))
        else:
            cutter = cq.Workplane().box(diag, diag, diag, centered=(False, True, True))
            cutter = cutter.translate((offset - diag, cy, cz))
    elif axis == "Y":
        if side == "above":
            cutter = cq.Workplane().box(diag, diag, diag, centered=(True, False, True))
            cutter = cutter.translate((cx, offset, cz))
        else:
            cutter = cq.Workplane().box(diag, diag, diag, centered=(True, False, True))
            cutter = cutter.translate((cx, offset - diag, cz))
    else:
        raise ValueError(f"axis must be X, Y or Z (got {axis!r})")

    return workplane.cut(cutter)


def _boolean(a, b, op: str):
    op = op.lower()
    if op == "union":
        return a.union(b)
    if op == "intersection":
        return a.intersect(b)
    if op == "difference":
        return a.cut(b)
    raise ValueError(f"unknown boolean op: {op!r}")


def main() -> int:
    spec_path = Path(sys.argv[1])
    png_out = Path(sys.argv[2])
    json_out = Path(sys.argv[3])

    result: dict = {"ok": False, "error": None}
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        try:
            spec_path.unlink()
        except OSError:
            pass

        loaded = [_load_item(item) for item in spec.get("items", [])]
        if not loaded:
            result["error"] = "scene spec has no items"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        post = spec.get("post")
        scene_items: list[dict]
        if not post or post.get("kind") in (None, "none"):
            # Render each loaded item with its own colour.
            from app.cad.snapshot import SCENE_COLORS
            scene_items = [
                {"shape": loaded[i]["model"],
                 "color": SCENE_COLORS[i % len(SCENE_COLORS)],
                 "opacity": 0.9 if len(loaded) > 1 else 1.0}
                for i in range(len(loaded))
            ]
        elif post.get("kind") == "section":
            sectioned = _section(
                loaded[0]["model"],
                post.get("axis", "Z"),
                float(post.get("offset", 0.0)),
                post.get("side", "above"),
            )
            scene_items = [{"shape": sectioned, "color": (0.78, 0.81, 0.86), "opacity": 1.0}]
        elif post.get("kind") == "boolean":
            by_name = {it["name"]: it["model"] for it in loaded}
            try:
                a = by_name[post["a"]]
                b = by_name[post["b"]]
            except KeyError as e:
                raise RuntimeError(f"boolean operand not in scene items: {e}") from e
            combined = _boolean(a, b, post.get("op", "union"))
            scene_items = [{"shape": combined, "color": (0.78, 0.81, 0.86), "opacity": 1.0}]
        else:
            raise ValueError(f"unknown post.kind: {post.get('kind')!r}")

        from app.cad.snapshot import render_scene
        png = render_scene(
            scene_items,
            spec.get("view"),
            width=int(spec.get("width", 900)),
            height=int(spec.get("height", 700)),
        )
        png_out.write_bytes(png)
        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()

    json_out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
