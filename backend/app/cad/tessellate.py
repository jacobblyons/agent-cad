"""Convert a CADQuery Workplane into a glTF binary (.glb) plus a
topology sidecar describing per-edge polylines and per-vertex points.

The glb carries one named node per face (`face_<i>`); the sidecar lets
the viewer build pickable line + point geometry for edges and vertices
without having to encode them inside the glb (trimesh is mesh-only).
"""
from __future__ import annotations

import base64
from typing import Any

import numpy as np
import trimesh

EDGE_SAMPLES = 24  # points per edge polyline


def _to_shape(workplane: Any):
    """Accept a Workplane, a Shape, or anything with .val()/.wrapped."""
    if hasattr(workplane, "val") and callable(workplane.val):
        return workplane.val()
    return workplane


def _face_to_trimesh(face, deflection: float) -> trimesh.Trimesh | None:
    verts, tris = face.tessellate(deflection)
    if not verts or not tris:
        return None
    v_arr = np.array([(v.x, v.y, v.z) for v in verts], dtype=np.float32)
    t_arr = np.array(tris, dtype=np.uint32)
    return trimesh.Trimesh(vertices=v_arr, faces=t_arr, process=False)


def _sample_edge(edge, samples: int = EDGE_SAMPLES) -> list[list[float]]:
    """Sample points along an edge using positionAt(t) on t ∈ [0, 1]."""
    pts: list[list[float]] = []
    for i in range(samples + 1):
        try:
            p = edge.positionAt(i / samples)
            pts.append([float(p.x), float(p.y), float(p.z)])
        except Exception:
            continue
    return pts


def _vertex_point(vertex) -> list[float] | None:
    try:
        c = vertex.Center()
        return [float(c.x), float(c.y), float(c.z)]
    except Exception:
        try:
            return [float(vertex.X), float(vertex.Y), float(vertex.Z)]
        except Exception:
            return None


def _safe_geom_type(entity) -> str | None:
    try:
        return entity.geomType()
    except Exception:
        return None


def to_glb(workplane: Any, deflection: float = 0.1) -> bytes:
    """Tessellate every face into one node per face; returns a glb."""
    shape = _to_shape(workplane)
    scene = trimesh.Scene()
    added = 0
    for i, face in enumerate(shape.Faces()):
        mesh = _face_to_trimesh(face, deflection)
        if mesh is None:
            continue
        scene.add_geometry(mesh, geom_name=f"face_{i}", node_name=f"face_{i}")
        added += 1
    if added == 0:
        verts, tris = shape.tessellate(deflection)
        v_arr = np.array([(v.x, v.y, v.z) for v in verts], dtype=np.float32)
        t_arr = np.array(tris, dtype=np.uint32)
        scene.add_geometry(trimesh.Trimesh(vertices=v_arr, faces=t_arr, process=False))
    return scene.export(file_type="glb")


def topology(workplane: Any) -> dict:
    """Per-edge polylines + per-vertex positions for picking + hover.

    Indices are positional in shape.Edges() / shape.Vertices() — the same
    order CADQuery returns them, which is the only stable mapping we have
    at this layer.
    """
    shape = _to_shape(workplane)
    edges: list[dict] = []
    for i, e in enumerate(shape.Edges()):
        points = _sample_edge(e)
        if len(points) < 2:
            continue
        edges.append({
            "index": i,
            "type": _safe_geom_type(e),
            "points": points,
        })
    vertices: list[dict] = []
    for i, v in enumerate(shape.Vertices()):
        p = _vertex_point(v)
        if p is None:
            continue
        vertices.append({"index": i, "point": p})
    faces_meta: list[dict] = []
    for i, f in enumerate(shape.Faces()):
        try:
            c = f.Center()
            faces_meta.append({
                "index": i,
                "type": _safe_geom_type(f),
                "centroid": [float(c.x), float(c.y), float(c.z)],
            })
        except Exception:
            faces_meta.append({"index": i, "type": _safe_geom_type(f)})
    return {"faces": faces_meta, "edges": edges, "vertices": vertices}


def to_glb_b64(workplane: Any, deflection: float = 0.1) -> str:
    return base64.b64encode(to_glb(workplane, deflection)).decode("ascii")
