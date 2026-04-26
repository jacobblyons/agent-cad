"""Shared import-loading helper used by every worker that needs to inject
the `imports` dict into an object script — _script_worker (run + GLB),
_snapshot_worker (PNG render), and _scene_worker (multi-object scene).

Imports are user-supplied reference models. Supported formats:

  STEP / STP        B-rep (preferred — full boolean + measurement support)
  IGES / IGS        B-rep (similar capability to STEP)
  BREP / BRP        OpenCascade native B-rep (text)
  STL               Mesh (DISPLAY-ONLY: bbox + viewer render work, but
                    boolean ops are unreliable because the geometry is
                    tessellated triangles, not analytic surfaces)
  GLB / GLTF        Mesh (same caveat as STL — glTF embeds triangles).
                    Sketchfab models, Blender exports, etc. are usually
                    in this format.
  3MF               Mesh (same caveat). Modern STL replacement, used by
                    Thingiverse, Printables, MakerBot Print, etc.; ZIP
                    of XML triangle data plus a build manifest.

Each loaded import becomes a `cq.Workplane` so object scripts can use it
through the injected `imports` dict.
"""
from __future__ import annotations

import json
from pathlib import Path

# Keep the canonical lists narrow but cover the important cases; the
# project layer mirrors this set when accepting files in.
BREP_EXTS = {".step", ".stp", ".iges", ".igs", ".brep", ".brp"}
MESH_EXTS = {".stl", ".glb", ".gltf", ".3mf"}
SUPPORTED_EXTS = BREP_EXTS | MESH_EXTS

# Hard cap on STL triangle count — converting a 1M-triangle STL to OCCT
# faces takes minutes and is rarely useful as a CAD reference. The cap
# protects the agent from hanging on an oversized scan.
STL_MAX_TRIANGLES = 60_000


def load_step_to_workplane(path: Path):
    """Read a STEP file and return a cq.Workplane wrapping its solid(s)."""
    import cadquery as cq
    return cq.importers.importStep(str(path))


def load_iges_to_workplane(path: Path):
    """Read an IGES file via OCCT's IGES reader. CADQuery doesn't have
    importIGES so we drive OCP directly, then wrap the result in a
    cq.Workplane the rest of the pipeline understands."""
    import cadquery as cq
    from OCP.IGESControl import IGESControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    reader = IGESControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"failed to read IGES file: {path}")
    reader.TransferRoots()
    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        raise RuntimeError(f"IGES file produced no shape: {path}")
    return cq.Workplane(obj=cq.Shape(shape))


def load_brep_to_workplane(path: Path):
    """Read an OpenCascade BREP file (text or binary)."""
    import cadquery as cq
    return cq.importers.importBrep(str(path))


def load_stl_to_workplane(path: Path):
    """Read an STL mesh into a cq.Workplane.

    Each triangle becomes a TopoDS_Face glued into a compound — this gives
    the rest of the pipeline (GLB tessellation, bbox, face count) something
    real to chew on, but boolean operations against the result are NOT
    reliable since triangulated faces are flat and adjacent faces only
    share endpoints, not edges.
    """
    import cadquery as cq
    from OCP.RWStl import RWStl
    from OCP.Message import Message_ProgressRange
    from OCP.TopoDS import TopoDS_Builder, TopoDS_Compound
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
    )

    triangulation = RWStl.ReadFile_s(str(path), Message_ProgressRange())
    if triangulation is None:
        raise RuntimeError(f"failed to read STL file: {path}")
    nb_tri = triangulation.NbTriangles()
    if nb_tri > STL_MAX_TRIANGLES:
        raise RuntimeError(
            f"STL has {nb_tri} triangles (cap is {STL_MAX_TRIANGLES}). "
            "Decimate the mesh before importing."
        )

    builder = TopoDS_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for i in range(1, nb_tri + 1):
        n1, n2, n3 = triangulation.Triangle(i).Get()
        p1 = triangulation.Node(n1)
        p2 = triangulation.Node(n2)
        p3 = triangulation.Node(n3)
        try:
            poly = BRepBuilderAPI_MakePolygon(p1, p2, p3, True).Wire()
            face = BRepBuilderAPI_MakeFace(poly).Face()
        except Exception:
            # Skip degenerate triangles rather than aborting the load.
            continue
        builder.Add(compound, face)
    return cq.Workplane(obj=cq.Shape(compound))


def load_gltf_to_workplane_dispatch(path: Path):
    """Lazy import wrapper — _gltf_loader pulls in numpy and OCP
    BRepBuilderAPI which aren't cheap, so only load the module when
    glTF is actually being used."""
    from . import _gltf_loader
    return _gltf_loader.load_gltf_to_workplane(path)


def load_3mf_to_workplane_dispatch(path: Path):
    """Lazy import wrapper — same rationale as the glTF dispatcher."""
    from . import _3mf_loader
    return _3mf_loader.load_3mf_to_workplane(path)


def load_to_workplane(path: Path):
    """Dispatch to the right loader based on the file's extension."""
    ext = path.suffix.lower()
    if ext in (".step", ".stp"):
        return load_step_to_workplane(path)
    if ext in (".iges", ".igs"):
        return load_iges_to_workplane(path)
    if ext in (".brep", ".brp"):
        return load_brep_to_workplane(path)
    if ext == ".stl":
        return load_stl_to_workplane(path)
    if ext in (".glb", ".gltf"):
        return load_gltf_to_workplane_dispatch(path)
    if ext == ".3mf":
        return load_3mf_to_workplane_dispatch(path)
    raise ValueError(f"unsupported import extension: {ext}")


def load_imports_from_manifest(manifest_path: Path | None) -> dict:
    """Resolve a manifest of imports into a {name: cq.Workplane} dict.

    A failing import (missing file, bad geometry, etc.) is silently
    skipped so that one corrupt reference file doesn't take down every
    object script that depends on a different one.
    """
    if manifest_path is None or not manifest_path.exists():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return {}
    if not isinstance(manifest, list) or not manifest:
        return {}

    out: dict = {}
    for entry in manifest:
        name = entry.get("name")
        path = Path(entry.get("path", ""))
        if not name or not path.exists():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTS:
            continue
        try:
            out[name] = load_to_workplane(path)
        except Exception:
            continue
    return out

