"""Minimal glTF / GLB loader for the imports pipeline.

OCP's RWGltf_CafReader bindings only expose the configuration setters,
not Perform/SetDocument, so we can't drive it from Python. Instead we
parse the GLB binary directly: it's a 12-byte header + chunked layout,
and we only need the embedded JSON + binary buffer to extract triangles.

The output is a cq.Workplane wrapping a TopoDS_Compound of triangle
faces — same shape as the STL loader. Mesh-only, so booleans against
the result aren't reliable, but bbox / face count / viewer rendering
all work, which is what the user wants when dropping in a Sketchfab
reference model.
"""
from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Iterable

import numpy as np

# Same cap as STL — converting a million-triangle scan into TopoDS_Faces
# is slow and rarely useful as a CAD reference.
GLTF_MAX_TRIANGLES = 60_000


# glTF componentType constants (from the spec).
_FLOAT = 5126
_UBYTE = 5121
_USHORT = 5123
_UINT = 5125

_INDEX_DTYPE = {
    _UBYTE: np.uint8,
    _USHORT: np.uint16,
    _UINT: np.uint32,
}

_GLB_MAGIC = b"glTF"
_CHUNK_JSON = 0x4E4F534A   # "JSON"
_CHUNK_BIN = 0x004E4942    # "BIN\0"


def _parse_glb(data: bytes) -> tuple[dict, bytes]:
    """Pull the JSON and binary chunks out of a GLB blob."""
    if len(data) < 12 or data[:4] != _GLB_MAGIC:
        raise ValueError("not a GLB file")
    _, length = struct.unpack("<II", data[4:12])
    if length > len(data):
        raise ValueError("GLB header length exceeds file size")
    pos = 12
    gltf_json: dict | None = None
    bin_buf = b""
    while pos < length:
        if pos + 8 > length:
            break
        clen, ctype = struct.unpack("<II", data[pos:pos + 8])
        cdata = data[pos + 8:pos + 8 + clen]
        if ctype == _CHUNK_JSON:
            gltf_json = json.loads(cdata.decode("utf-8").rstrip("\x00"))
        elif ctype == _CHUNK_BIN:
            bin_buf = bytes(cdata)
        pos += 8 + clen
    if gltf_json is None:
        raise ValueError("GLB has no JSON chunk")
    return gltf_json, bin_buf


def _parse_text_gltf(path: Path) -> tuple[dict, bytes]:
    """Read a text .gltf file plus its referenced binary buffer."""
    gltf = json.loads(path.read_text(encoding="utf-8"))
    buffers = gltf.get("buffers") or []
    bin_buf = b""
    if buffers:
        b0 = buffers[0]
        uri = b0.get("uri", "")
        if uri.startswith("data:"):
            # data:application/octet-stream;base64,XXX
            comma = uri.find(",")
            if comma >= 0:
                import base64
                bin_buf = base64.b64decode(uri[comma + 1:])
        elif uri:
            bin_buf = (path.parent / uri).read_bytes()
    return gltf, bin_buf


def _accessor_array(gltf: dict, bin_buf: bytes, accessor_idx: int,
                    expected_type: str | None = None) -> np.ndarray:
    """Extract one accessor's data as a numpy array. Handles vec3 FLOAT
    and scalar UBYTE/USHORT/UINT; we don't support sparse accessors yet
    (none of the Sketchfab CAD models seem to use them)."""
    a = gltf["accessors"][accessor_idx]
    if expected_type and a.get("type") != expected_type:
        raise ValueError(
            f"accessor {accessor_idx} type {a.get('type')!r} != expected {expected_type!r}"
        )
    bv = gltf["bufferViews"][a["bufferView"]]
    offset = int(bv.get("byteOffset", 0)) + int(a.get("byteOffset", 0))
    count = int(a["count"])
    ct = int(a["componentType"])

    if a.get("type") == "VEC3" and ct == _FLOAT:
        size = count * 3 * 4
        return np.frombuffer(bin_buf[offset:offset + size], dtype=np.float32).reshape(-1, 3)
    if a.get("type") == "SCALAR" and ct in _INDEX_DTYPE:
        dtype = _INDEX_DTYPE[ct]
        size = count * dtype().itemsize
        return np.frombuffer(bin_buf[offset:offset + size], dtype=dtype).astype(np.int64)
    raise ValueError(
        f"unsupported accessor: type={a.get('type')!r} componentType={ct}"
    )


def _node_local_matrix(node: dict) -> np.ndarray:
    """4x4 matrix for one node, either from explicit `matrix` or
    decomposed translation/rotation/scale."""
    m = node.get("matrix")
    if m is not None and len(m) == 16:
        # glTF stores matrices column-major; numpy is row-major. Transpose
        # so the standard `out @ in` order gives the expected result.
        return np.array(m, dtype=np.float64).reshape(4, 4).T
    out = np.eye(4, dtype=np.float64)
    s = node.get("scale")
    if s and len(s) == 3:
        out[0, 0] = s[0]; out[1, 1] = s[1]; out[2, 2] = s[2]
    r = node.get("rotation")
    if r and len(r) == 4:
        # quaternion (x, y, z, w) → 3x3 rotation
        x, y, z, w = (float(c) for c in r)
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w),     0.0],
            [2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w),     0.0],
            [2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])
        out = rot @ out
    t = node.get("translation")
    if t and len(t) == 3:
        out[0, 3] += float(t[0])
        out[1, 3] += float(t[1])
        out[2, 3] += float(t[2])
    return out


def _iter_triangles(gltf: dict, bin_buf: bytes) -> Iterable[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Walk the default scene's node tree, yielding world-space triangle
    vertex triples. Skips primitives that aren't TRIANGLES (mode 4) since
    rendering line / point primitives as faces doesn't make sense."""
    scenes = gltf.get("scenes") or []
    if not scenes:
        return
    scene_idx = int(gltf.get("scene", 0))
    scene = scenes[scene_idx] if scene_idx < len(scenes) else scenes[0]

    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []

    def visit(node_idx: int, parent: np.ndarray):
        node = nodes[node_idx]
        local = _node_local_matrix(node)
        world = parent @ local
        if "mesh" in node:
            mesh = meshes[int(node["mesh"])]
            for prim in mesh.get("primitives") or []:
                if int(prim.get("mode", 4)) != 4:
                    continue
                attrs = prim.get("attributes") or {}
                pos_idx = attrs.get("POSITION")
                if pos_idx is None:
                    continue
                positions = _accessor_array(gltf, bin_buf, int(pos_idx), "VEC3")
                # Apply the world matrix to positions: out = (R, 1) * (p, 1)
                ones = np.ones((positions.shape[0], 1), dtype=np.float64)
                hpts = np.hstack((positions.astype(np.float64), ones))
                world_pts = (world @ hpts.T).T[:, :3]
                if "indices" in prim:
                    indices = _accessor_array(gltf, bin_buf, int(prim["indices"]))
                    for i in range(0, len(indices) - 2, 3):
                        a, b, c = indices[i], indices[i + 1], indices[i + 2]
                        yield world_pts[a], world_pts[b], world_pts[c]
                else:
                    for i in range(0, len(world_pts) - 2, 3):
                        yield world_pts[i], world_pts[i + 1], world_pts[i + 2]
        for child in node.get("children") or []:
            yield from visit(int(child), world)

    root = np.eye(4, dtype=np.float64)
    for n in scene.get("nodes") or []:
        yield from visit(int(n), root)


def _read_gltf_or_glb(path: Path) -> tuple[dict, bytes]:
    if path.suffix.lower() == ".glb":
        return _parse_glb(path.read_bytes())
    return _parse_text_gltf(path)


def gltf_bbox(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Compute the world-space bounding box of every triangle in a glTF
    file. Cheap because we don't build OCCT geometry."""
    gltf, bin_buf = _read_gltf_or_glb(path)
    mins = np.array([np.inf, np.inf, np.inf])
    maxs = np.array([-np.inf, -np.inf, -np.inf])
    found = False
    for p0, p1, p2 in _iter_triangles(gltf, bin_buf):
        for p in (p0, p1, p2):
            mins = np.minimum(mins, p)
            maxs = np.maximum(maxs, p)
            found = True
    if not found:
        raise RuntimeError("glTF file has no triangle geometry")
    return tuple(mins.tolist()), tuple(maxs.tolist())


def load_gltf_to_workplane(path: Path):
    """Build a cq.Workplane wrapping a compound of triangle faces, with
    every node transform baked in. Capped at GLTF_MAX_TRIANGLES so a
    huge mesh doesn't hang the import."""
    import cadquery as cq
    from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
    )
    from OCP.gp import gp_Pnt

    gltf, bin_buf = _read_gltf_or_glb(path)

    builder = TopoDS_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    n_added = 0
    for p0, p1, p2 in _iter_triangles(gltf, bin_buf):
        if n_added >= GLTF_MAX_TRIANGLES:
            raise RuntimeError(
                f"glTF has more than {GLTF_MAX_TRIANGLES} triangles. "
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
        raise RuntimeError("glTF file produced no usable triangles")
    return cq.Workplane(obj=cq.Shape(compound))
