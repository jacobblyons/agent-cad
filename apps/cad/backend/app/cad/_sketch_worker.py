"""Subprocess entrypoint: run a sketch script and emit its wires as
3D-projected polylines suitable for line rendering in the viewer.

Argv:
    1 = script_path     (sketch .py file defining `sketch` and optional `plane`)
    2 = params_path     (params JSON; may be missing/empty)
    3 = output json path (always written; ok=True/False)
    4 = sample tolerance for curved edges (float, mm)

Output JSON shape on success:
    {
      "ok": true,
      "plane": {"origin": [x,y,z], "x_dir": [...], "y_dir": [...], "normal": [...]},
      "polylines": [
        {"points": [[x,y,z], ...], "closed": true|false},
        ...
      ],
      "dimensions": [
        {"kind": "length"|"radius", "value": float, "anchor": [x,y,z]},
        ...
      ],
      "bbox": {"min": [...], "max": [...]}
    }
"""
from __future__ import annotations

import json
import math
import runpy
import sys
import traceback
from pathlib import Path


def _to_world(local, plane) -> list[float]:
    try:
        w = plane.toWorldCoords((float(local.x), float(local.y)))
        return [float(w.x), float(w.y), float(w.z)]
    except Exception:
        return [float(local.x), float(local.y), float(getattr(local, "z", 0.0))]


def _edge_dimension(edge, plane) -> dict | None:
    """One dimension entry per edge. LINE / ARC / spline → length at midpoint;
    full CIRCLE → radius at center. Returns None for degenerate edges so
    near-zero-length strokes don't pollute the overlay."""
    geom = ""
    try:
        geom = (edge.geomType() or "").upper()
    except Exception:
        pass
    try:
        length = float(edge.Length())
    except Exception:
        return None
    if length <= 1e-4:
        return None

    # Full circle: start ≈ end, single edge wraps around. Emit the radius
    # at the geometric center of the sampled span instead of the midpoint
    # (which would be the antipode of the seam).
    if geom == "CIRCLE":
        try:
            sp = edge.startPoint()
            ep = edge.endPoint()
            if (abs(sp.x - ep.x) < 1e-6 and abs(sp.y - ep.y) < 1e-6
                    and abs(getattr(sp, "z", 0.0) - getattr(ep, "z", 0.0)) < 1e-6):
                radius = length / (2.0 * math.pi)
                # Center ≈ average of two opposite samples on the circle.
                try:
                    a = edge.positionAt(0.0)
                    b = edge.positionAt(0.5)
                    cx = 0.5 * (float(a.x) + float(b.x))
                    cy = 0.5 * (float(a.y) + float(b.y))
                    center_local = type("P", (), {"x": cx, "y": cy})()
                    return {
                        "kind": "radius",
                        "value": radius,
                        "anchor": _to_world(center_local, plane),
                    }
                except Exception:
                    pass
        except Exception:
            pass

    try:
        mid = edge.positionAt(0.5)
    except Exception:
        return None
    return {
        "kind": "length",
        "value": length,
        "anchor": _to_world(mid, plane),
    }


def _sample_edge(edge, plane, tol: float) -> list[list[float]]:
    """Return points along an edge, projected from the sketch's local 2D
    frame into world coordinates via `plane`.

    Uses the edge's own length to pick a sample count: a 100mm circle sampled
    at 0.5mm tol → 200 points, a straight line at any tol → just endpoints.
    """
    geom = ""
    try:
        geom = (edge.geomType() or "").upper()
    except Exception:
        pass
    try:
        length = float(edge.Length())
    except Exception:
        length = 0.0

    # Straight line: endpoints are enough.
    if geom == "LINE":
        try:
            sp = edge.startPoint()
            ep = edge.endPoint()
            return [_to_world(sp, plane), _to_world(ep, plane)]
        except Exception:
            pass

    if length <= 0.0:
        return []
    n = max(8, int(math.ceil(length / max(tol, 0.05))))
    n = min(n, 256)
    pts: list[list[float]] = []
    for i in range(n + 1):
        u = i / n
        try:
            p = edge.positionAt(u)
            pts.append(_to_world(p, plane))
        except Exception:
            continue
    return pts


def main() -> int:
    script_path = Path(sys.argv[1])
    params_path = Path(sys.argv[2])
    json_out = Path(sys.argv[3])
    tol = float(sys.argv[4]) if len(sys.argv) > 4 else 0.5

    result: dict = {"ok": False, "error": None}
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

        globs = runpy.run_path(str(script_path), init_globals={"params": params})
        sketch = globs.get("sketch")
        if sketch is None:
            result["error"] = f"{script_path.name} finished without defining `sketch`"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0
        plane = globs.get("plane", "XY")

        import cadquery as cq
        from app.cad._sketch_loader import build_workplane_from_plane
        wp = build_workplane_from_plane(cq, plane).placeSketch(sketch)
        # `wp.val()` after placeSketch returns a cq.Sketch (not a Compound),
        # so we can't walk Faces() on it directly. Instead, get the sketch's
        # own faces (in its local 2D frame, XY by default) and transform
        # every sampled point into world coords via the workplane's plane.
        plane_obj = wp.plane

        local_faces: list = []
        try:
            local_faces = sketch.faces().vals()
        except Exception:
            local_faces = []

        polylines: list[dict] = []
        dimensions: list[dict] = []
        xmin = ymin = zmin = float("inf")
        xmax = ymax = zmax = float("-inf")

        def update_bbox(p: list[float]) -> None:
            nonlocal xmin, ymin, zmin, xmax, ymax, zmax
            xmin = min(xmin, p[0]); ymin = min(ymin, p[1]); zmin = min(zmin, p[2])
            xmax = max(xmax, p[0]); ymax = max(ymax, p[1]); zmax = max(zmax, p[2])

        def collect_edge(edge) -> list[list[float]]:
            dim = _edge_dimension(edge, plane_obj)
            if dim is not None:
                dimensions.append(dim)
            return _sample_edge(edge, plane_obj, tol)

        if local_faces:
            for face in local_faces:
                for wire in face.Wires():
                    pts: list[list[float]] = []
                    for edge in wire.Edges():
                        seg = collect_edge(edge)
                        # Avoid duplicating the join point between consecutive
                        # edges in the same wire.
                        if pts and seg and pts[-1] == seg[0]:
                            seg = seg[1:]
                        pts.extend(seg)
                    if not pts:
                        continue
                    closed = False
                    try:
                        closed = bool(wire.IsClosed())
                    except Exception:
                        pass
                    polylines.append({"points": pts, "closed": closed})
                    for p in pts:
                        update_bbox(p)
        else:
            # Fallback for open sketches: walk every edge from the raw
            # _faces compound (or _wires if present).
            try:
                raw = getattr(sketch, "_faces", None)
                edges = raw.Edges() if raw is not None and hasattr(raw, "Edges") else []
            except Exception:
                edges = []
            for edge in edges:
                pts = collect_edge(edge)
                if pts:
                    polylines.append({"points": pts, "closed": False})
                    for p in pts:
                        update_bbox(p)

        if not polylines:
            result["error"] = "sketch produced no renderable geometry"
            json_out.write_text(json.dumps(result), encoding="utf-8")
            return 0

        # Frame info — handy for the agent and for any future viewer overlay
        # that wants to show the sketch's plane axes.
        try:
            origin = plane_obj.origin
            xdir = plane_obj.xDir
            ydir = plane_obj.yDir
            normal = plane_obj.zDir
            plane_info = {
                "origin": [origin.x, origin.y, origin.z],
                "x_dir": [xdir.x, xdir.y, xdir.z],
                "y_dir": [ydir.x, ydir.y, ydir.z],
                "normal": [normal.x, normal.y, normal.z],
            }
        except Exception:
            plane_info = None

        result.update({
            "ok": True,
            "plane": plane_info,
            "polylines": polylines,
            "dimensions": dimensions,
            "bbox": {
                "min": [xmin, ymin, zmin],
                "max": [xmax, ymax, zmax],
            },
        })
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["trace"] = traceback.format_exc()

    json_out.write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
