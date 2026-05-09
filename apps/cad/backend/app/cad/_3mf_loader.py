"""Minimal 3MF loader for the imports pipeline.

3MF is ZIP + XML. The single file we care about lives at
`3D/3dmodel.model` inside the zip; it lists `<object>` resources (each
either a `<mesh>` of vertices+triangles or a `<components>` list of
references to other objects with transforms) and a `<build>` section
of items the app should actually instantiate (each item also with an
optional transform).

OCP doesn't ship 3MF Python bindings so we parse the format ourselves.
The XML schema we care about is small enough to consume with stdlib
xml.etree.

The output is a cq.Workplane wrapping a TopoDS_Compound of triangle
faces — same mesh-import shape as STL / glTF, with the same boolean
caveats. A 3MF unit attribute (millimeter / centimeter / inch / etc.)
is applied as a uniform scale so models authored in non-mm units come
out at the right size.
"""
from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

import numpy as np

THREEMF_MAX_TRIANGLES = 60_000

# 3MF core spec namespace.
NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
_NS = f"{{{NS}}}"

# Per the spec, units are one of: micron, millimeter, centimeter, inch,
# foot, meter. Default is millimeter. We convert everything to mm.
UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "inch": 25.4,
    "foot": 304.8,
    "meter": 1000.0,
}


def _parse_transform(s: str | None) -> np.ndarray:
    """3MF transform attribute is 12 floats: m11 m12 m13 m21 m22 m23 m31
    m32 m33 m41 m42 m43.  Each column is a basis vector / translation,
    the implicit fourth column is (0,0,0,1). Returns a 4x4 numpy matrix
    suitable for `m @ p`."""
    if not s:
        return np.eye(4, dtype=np.float64)
    parts = s.split()
    if len(parts) != 12:
        return np.eye(4, dtype=np.float64)
    try:
        n = [float(p) for p in parts]
    except ValueError:
        return np.eye(4, dtype=np.float64)
    return np.array([
        [n[0], n[3], n[6], n[9]],
        [n[1], n[4], n[7], n[10]],
        [n[2], n[5], n[8], n[11]],
        [0.0, 0.0, 0.0, 1.0],
    ], dtype=np.float64)


def _read_root(path: Path) -> tuple[ET.Element, str]:
    """Open the .3mf zip and parse the main model XML. Returns the root
    element and the unit string."""
    with zipfile.ZipFile(path, "r") as z:
        # Standard model location. Some authoring tools put it elsewhere
        # via the [Content_Types] / .rels indirection, but the canonical
        # path covers ~every file we'd see in practice.
        candidates = ("3D/3dmodel.model", "3d/3dmodel.model")
        member: str | None = None
        for c in candidates:
            if c in z.namelist():
                member = c
                break
        if member is None:
            # Last resort: any *.model file inside.
            for n in z.namelist():
                if n.lower().endswith(".model"):
                    member = n
                    break
        if member is None:
            raise RuntimeError("3MF archive has no model file")
        with z.open(member) as f:
            root = ET.parse(f).getroot()
    unit = root.attrib.get("unit", "millimeter")
    return root, unit


def _parse_resources(root: ET.Element) -> dict[str, dict]:
    """Build {object_id: {mesh: (verts, tris)|None, components: [(id, xform)]}}."""
    out: dict[str, dict] = {}
    res = root.find(f"{_NS}resources")
    if res is None:
        return out
    for obj in res.findall(f"{_NS}object"):
        oid = obj.attrib.get("id")
        if not oid:
            continue
        entry: dict = {"mesh": None, "components": []}
        mesh = obj.find(f"{_NS}mesh")
        if mesh is not None:
            verts: list[tuple[float, float, float]] = []
            tris: list[tuple[int, int, int]] = []
            v_node = mesh.find(f"{_NS}vertices")
            if v_node is not None:
                for v in v_node.findall(f"{_NS}vertex"):
                    try:
                        verts.append((
                            float(v.attrib["x"]),
                            float(v.attrib["y"]),
                            float(v.attrib["z"]),
                        ))
                    except (KeyError, ValueError):
                        continue
            t_node = mesh.find(f"{_NS}triangles")
            if t_node is not None:
                for t in t_node.findall(f"{_NS}triangle"):
                    try:
                        tris.append((
                            int(t.attrib["v1"]),
                            int(t.attrib["v2"]),
                            int(t.attrib["v3"]),
                        ))
                    except (KeyError, ValueError):
                        continue
            entry["mesh"] = (verts, tris)
        comps = obj.find(f"{_NS}components")
        if comps is not None:
            for c in comps.findall(f"{_NS}component"):
                cid = c.attrib.get("objectid")
                if not cid:
                    continue
                entry["components"].append((cid, _parse_transform(c.attrib.get("transform"))))
        out[oid] = entry
    return out


def _parse_build(root: ET.Element) -> list[tuple[str, np.ndarray]]:
    out: list[tuple[str, np.ndarray]] = []
    b = root.find(f"{_NS}build")
    if b is None:
        return out
    for item in b.findall(f"{_NS}item"):
        oid = item.attrib.get("objectid")
        if not oid:
            continue
        out.append((oid, _parse_transform(item.attrib.get("transform"))))
    return out


def _iter_triangles(
    resources: dict[str, dict],
    build_items: list[tuple[str, np.ndarray]],
    unit_scale: float,
) -> Iterable[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Yield world-space triangle vertex triples, applying the build
    item's transform, the components' transforms, and the file's unit
    scale."""

    def visit(obj_id: str, parent: np.ndarray, visited: set[str]):
        if obj_id in visited or obj_id not in resources:
            return
        visited = visited | {obj_id}
        entry = resources[obj_id]
        mesh = entry.get("mesh")
        if mesh:
            verts, tris = mesh
            if not verts or not tris:
                pass
            else:
                v_arr = np.array(verts, dtype=np.float64)
                # Apply transform: (parent @ [v; 1])[:3]
                ones = np.ones((v_arr.shape[0], 1), dtype=np.float64)
                hpts = np.hstack((v_arr, ones))
                world = (parent @ hpts.T).T[:, :3] * unit_scale
                for v1, v2, v3 in tris:
                    if v1 < len(world) and v2 < len(world) and v3 < len(world):
                        yield world[v1], world[v2], world[v3]
        for cid, ctform in entry.get("components") or []:
            yield from visit(cid, parent @ ctform, visited)

    for oid, xform in build_items:
        yield from visit(oid, xform, set())


def threemf_bbox(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    root, unit = _read_root(path)
    unit_scale = UNIT_TO_MM.get(unit.lower(), 1.0)
    resources = _parse_resources(root)
    build_items = _parse_build(root)
    mins = np.array([np.inf, np.inf, np.inf])
    maxs = np.array([-np.inf, -np.inf, -np.inf])
    found = False
    for p0, p1, p2 in _iter_triangles(resources, build_items, unit_scale):
        for p in (p0, p1, p2):
            mins = np.minimum(mins, p)
            maxs = np.maximum(maxs, p)
            found = True
    if not found:
        raise RuntimeError("3MF file has no triangle geometry")
    return tuple(mins.tolist()), tuple(maxs.tolist())


def load_3mf_to_workplane(path: Path):
    import cadquery as cq
    from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
    )
    from OCP.gp import gp_Pnt

    root, unit = _read_root(path)
    unit_scale = UNIT_TO_MM.get(unit.lower(), 1.0)
    resources = _parse_resources(root)
    build_items = _parse_build(root)

    builder = TopoDS_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    n_added = 0
    for p0, p1, p2 in _iter_triangles(resources, build_items, unit_scale):
        if n_added >= THREEMF_MAX_TRIANGLES:
            raise RuntimeError(
                f"3MF has more than {THREEMF_MAX_TRIANGLES} triangles. "
                "Decimate the mesh before importing."
            )
        try:
            poly = BRepBuilderAPI_MakePolygon(
                gp_Pnt(float(p0[0]), float(p0[1]), float(p0[2])),
                gp_Pnt(float(p1[0]), float(p1[1]), float(p1[2])),
                gp_Pnt(float(p2[0]), float(p2[1]), float(p2[2])),
                True,
            ).Wire()
            face = BRepBuilderAPI_MakeFace(poly).Face()
        except Exception:
            continue
        builder.Add(compound, face)
        n_added += 1

    if n_added == 0:
        raise RuntimeError("3MF file produced no usable triangles")
    return cq.Workplane(obj=cq.Shape(compound))
