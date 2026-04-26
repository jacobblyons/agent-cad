"""CAD tool surface for the agent.

The agent has the SDK's built-in Read/Write/Edit/Glob/Grep on the project
directory and a CAD-specific surface defined here. Most script-level
tools (run_model, snapshot, measure, eval_expression, set_parameter, ...)
operate on the *active* object of the project. Cross-object and
evaluation tools (distance_between, preview_boolean, scene_snapshot)
take explicit object names or entity refs.

Entity refs (used by distance_between):
    "main"              the whole shape of object 'main'
    "main.face[7]"      face index 7 of 'main'
    "main.edge[3]"      edge index 3 of 'main'
    "main.vertex[0]"    vertex index 0 of 'main'
    ".face[7]"          (no object prefix) face 7 of the active object
"""
from __future__ import annotations

import base64
import json
import re
import runpy
import traceback
from typing import Any, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from ..cad.project import Project
from ..cad.script_runner import (
    RunResult,
    run as run_script,
    scene as scene_script,
    snapshot as snapshot_script,
    tessellate_sketch as tessellate_sketch_script,
)
from ..events import bus


def _sketches_manifest(project: Project) -> list[dict]:
    """Manifest entries for every sketch in the project — same shape as
    api._sketches_manifest, kept here so the agent's subprocess runs see
    the same sketch set the live viewer sees."""
    return [
        {
            "name": s["name"],
            "script": str(project.sketch_source_path(s["name"])),
            "params": str(project.sketch_params_path(s["name"])),
        }
        for s in project.list_sketches()
    ]


def _ok(text: str, extra: list[dict] | None = None) -> dict:
    content: list[dict] = [{"type": "text", "text": text}]
    if extra:
        content.extend(extra)
    return {"content": content}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _image_block(png_bytes: bytes) -> dict:
    return {
        "type": "image",
        "data": base64.b64encode(png_bytes).decode("ascii"),
        "mimeType": "image/png",
    }


_REF_PATTERN = re.compile(
    r"^([A-Za-z][A-Za-z0-9_\-]*)?(?:\.(face|edge|vertex)\[(\d+)\])?$"
)


def _vec3(x: Any, name: str) -> list[float] | None:
    """Coerce a 3-element iterable of numbers; returns None if missing."""
    if x is None:
        return None
    if not isinstance(x, (list, tuple)) or len(x) != 3:
        raise ValueError(f"{name!r} must be a length-3 list of numbers, got {x!r}")
    try:
        return [float(x[0]), float(x[1]), float(x[2])]
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name!r} must be numeric, got {x!r}") from e


def _build_view_arg(view_name: str | None, camera: Any) -> dict:
    """Pick between a preset preset (string) and an explicit camera pose.

    Explicit `camera` wins when supplied so the agent can reproduce the same
    angle the user saw when they took an annotated screenshot.
    """
    if isinstance(camera, dict) and (camera.get("position") or camera.get("target")):
        position = _vec3(camera.get("position"), "camera.position")
        target = _vec3(camera.get("target"), "camera.target")
        up = _vec3(camera.get("up"), "camera.up") or [0.0, 0.0, 1.0]
        if not position or not target:
            raise ValueError("camera requires both 'position' and 'target' (length-3 lists)")
        return {"position": position, "target": target, "up": up}
    return {"preset": (view_name or "iso").strip().lower()}


class CadToolset:
    """Per-chat-turn handle the tools share.

    Caches loaded `model` globals per object so back-to-back tools that
    don't change anything don't re-execute the script. Cache entries are
    invalidated whenever any sketch changes (since object scripts can
    consume sketches and a stale cached model would hide the change).
    """

    def __init__(self, project: Project, render: Callable[[RunResult], None]):
        self.project = project
        self.render = render
        self._model_cache: dict[str, tuple] = {}
        self._sketch_cache: dict[str, tuple] = {}

    def _sketches_dict_in_process(self) -> dict:
        """Run every sketch script in-process, returning a {name: cq.Workplane}
        dict ready to be injected into an object script. Mirrors what
        `_sketch_loader.load_sketches_from_manifest` does in the subprocess
        runners — both must stay in sync."""
        import cadquery as cq
        from ..cad._sketch_loader import build_workplane_from_plane
        out: dict = {}
        for s in self.project.list_sketches():
            name = s["name"]
            script = self.project.sketch_source_path(name)
            if not script.exists():
                continue
            params = self.project.read_sketch_params(name)
            try:
                globs = runpy.run_path(str(script), init_globals={"params": params})
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

    def _sketches_signature(self) -> tuple:
        """A hashable fingerprint of every sketch's source + params, used as
        part of the model-cache key so a sketch change re-runs dependents."""
        parts = []
        for s in self.project.list_sketches():
            name = s["name"]
            try:
                mtime = self.project.sketch_source_path(name).stat().st_mtime
            except OSError:
                mtime = 0.0
            params_blob = json.dumps(self.project.read_sketch_params(name), sort_keys=True)
            parts.append((name, mtime, params_blob))
        return tuple(parts)

    def load_object_in_process(self, name: str):
        """Run any object's script in this process and return its `model`."""
        script = self.project.object_source_path(name)
        if not script.exists():
            raise FileNotFoundError(f"object {name!r} does not exist")
        st = script.stat().st_mtime
        params = self.project.read_object_params(name)
        sketch_sig = self._sketches_signature()
        sig = (st, json.dumps(params, sort_keys=True), sketch_sig)
        cached = self._model_cache.get(name)
        if cached and cached[0] == sig:
            return cached[1]
        sketches = self._sketches_dict_in_process()
        globs = runpy.run_path(
            str(script),
            init_globals={"params": params, "sketches": sketches},
        )
        model = globs.get("model")
        if model is None:
            raise RuntimeError(f"{script.name} finished without defining `model`")
        self._model_cache[name] = (sig, model)
        return model

    def load_sketch_in_process(self, name: str):
        """Run a sketch's script and return the placed `cq.Workplane`. Useful
        for tools that want to inspect sketch geometry without needing a
        full subprocess."""
        if not self.project.sketch_exists(name):
            raise FileNotFoundError(f"sketch {name!r} does not exist")
        script = self.project.sketch_source_path(name)
        st = script.stat().st_mtime
        params = self.project.read_sketch_params(name)
        sig = (st, json.dumps(params, sort_keys=True))
        cached = self._sketch_cache.get(name)
        if cached and cached[0] == sig:
            return cached[1]
        import cadquery as cq
        from ..cad._sketch_loader import build_workplane_from_plane
        globs = runpy.run_path(str(script), init_globals={"params": params})
        sketch = globs.get("sketch")
        if sketch is None:
            raise RuntimeError(f"{script.name} finished without defining `sketch`")
        plane = globs.get("plane", "XY")
        wp = build_workplane_from_plane(cq, plane).placeSketch(sketch)
        self._sketch_cache[name] = (sig, wp)
        return wp

    def load_model_in_process(self):
        """Active object's model — convenience for tools that don't take an object name."""
        return self.load_object_in_process(self.project.active_object())

    def invalidate(self) -> None:
        self._model_cache.clear()
        self._sketch_cache.clear()

    def resolve_entity_ref(self, ref: str):
        """Resolve a ref like 'main', 'main.face[7]', '.edge[3]' to a CADQuery shape."""
        ref = (ref or "").strip()
        m = _REF_PATTERN.match(ref)
        if not m:
            raise ValueError(
                f"could not parse entity ref {ref!r}; "
                "expected 'name', 'name.face[i]', 'name.edge[i]', or 'name.vertex[i]'"
            )
        obj_name, kind, idx = m.group(1), m.group(2), m.group(3)
        if not obj_name:
            obj_name = self.project.active_object()
        model = self.load_object_in_process(obj_name)
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        if kind is None:
            return shape
        idx_i = int(idx)
        if kind == "face":
            items = shape.Faces()
        elif kind == "edge":
            items = shape.Edges()
        else:
            items = shape.Vertices()
        if idx_i < 0 or idx_i >= len(items):
            raise ValueError(
                f"{kind} index {idx_i} out of range (0..{len(items) - 1}) for object {obj_name!r}"
            )
        return items[idx_i]


def build_cad_server(toolset: CadToolset) -> Any:
    """Construct an in-process MCP server bound to the given toolset."""
    return create_sdk_mcp_server(
        name="cad",
        version="0.4.0",
        tools=build_cad_tools(toolset),
    )


def build_cad_tools(toolset: CadToolset) -> list[Any]:
    """Build the list of SdkMcpTool objects for the given toolset.

    Exposed separately from ``build_cad_server`` so tests can call the
    underlying async handlers directly via tool.handler(args).
    """

    # ===== core: run / snapshot / measure / params =====================

    @tool(
        "run_model",
        "Execute the active artifact's script in a sandboxed subprocess. ALWAYS call this after editing the script to verify it parses. "
        "If the active artifact is an OBJECT, runs its script (with all sketches injected as the `sketches` dict) and pushes the resulting GLB to the viewer. "
        "If the active artifact is a SKETCH, runs the sketch script and pushes its 2D wires (projected onto its plane in 3D) to the viewer's overlay layer so the user can see the sketch. "
        "Returns ok/error and a brief summary. Errors include the Python traceback so you can fix them.",
        {},
    )
    async def run_model(args):
        toolset.invalidate()
        proj = toolset.project
        kind, name = proj.active_artifact()

        if kind == "sketch":
            sk = tessellate_sketch_script(
                proj.sketch_source_path(name),
                proj.sketch_params_path(name),
                cwd=proj.path,
                timeout=20.0,
            )
            # Push the sketch overlay event ourselves; the runner-level
            # render callback is shaped for object RunResult.
            bus.emit("doc_sketch_geometry", {
                "doc_id": proj.id, "sketch": name,
                "ok": sk.ok, "error": sk.error, "stderr": sk.stderr,
                "plane": sk.plane, "polylines": sk.polylines, "bbox": sk.bbox,
            })
            bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
            if not sk.ok:
                msg = sk.error or "sketch failed"
                if sk.stderr:
                    msg += "\n\nstderr:\n" + sk.stderr.strip()[-1500:]
                return _err(msg)
            n_lines = len(sk.polylines or [])
            n_pts = sum(len(p.get("points") or []) for p in (sk.polylines or []))
            return _ok(f"OK (sketch {name}). {n_lines} polyline(s), {n_pts} sample points.")

        result = run_script(
            proj.object_source_path(name),
            proj.object_params_path(name),
            cwd=proj.path,
            timeout=30.0,
            sketches=_sketches_manifest(proj),
        )
        toolset.render(result)
        if result.ok:
            m = result.meta or {}
            bb = m.get("bbox", {})
            summary = (
                f"OK ({name}). bbox size: {bb.get('size')}, "
                f"volume: {m.get('volume'):.2f} mm^3, "
                f"faces: {m.get('face_count')}, edges: {m.get('edge_count')}."
            )
            return _ok(summary)
        msg = result.error or "model failed"
        if result.stderr:
            msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
        return _err(msg)

    @tool(
        "snapshot",
        "Render a PNG of the active object from the chosen viewpoint and return it as an image you can look at. "
        "view: 'iso' | 'front' | 'back' | 'left' | 'right' | 'top' | 'bottom'. "
        "OR pass an explicit camera pose to match a specific angle: "
        "camera={'position':[x,y,z], 'target':[x,y,z], 'up':[x,y,z]} in CADQuery coords (mm, +Z up). "
        "When the user attaches an annotated screenshot, ALWAYS use the camera "
        "pose listed in its description so your verification render is from the "
        "same angle — preset views routinely hide the feature the user circled. "
        "Use this liberally to verify your edits look right — you are multimodal and can see the result.",
        {"view": str, "camera": dict},
    )
    async def snapshot(args):
        proj = toolset.project
        active = proj.active_object()
        try:
            view_dict = _build_view_arg(args.get("view"), args.get("camera"))
        except ValueError as e:
            return _err(str(e))
        result = snapshot_script(
            proj.object_source_path(active),
            proj.object_params_path(active),
            view_dict,
            cwd=proj.path,
            width=900, height=700, timeout=30.0,
            sketches=_sketches_manifest(proj),
        )
        if not result.ok:
            msg = result.error or "snapshot failed"
            if result.stderr:
                msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
            return _err(msg)
        label = view_dict.get("preset") or (
            f"camera pos={view_dict['position']} target={view_dict['target']}"
        )
        return {
            "content": [
                {"type": "text", "text": f"snapshot of '{active}' from {label}:"},
                _image_block(result.png_bytes or b""),
            ]
        }

    @tool(
        "measure",
        "Return overall measurements of the active object: bounding box (min, max, size, diagonal), volume (mm^3), surface area (mm^2), and entity counts.",
        {},
    )
    async def measure(args):
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        bb = shape.BoundingBox()
        info = {
            "bbox": {
                "min": [bb.xmin, bb.ymin, bb.zmin],
                "max": [bb.xmax, bb.ymax, bb.zmax],
                "size": [bb.xlen, bb.ylen, bb.zlen],
                "diagonal": bb.DiagonalLength,
            },
            "volume_mm3": shape.Volume(),
            "area_mm2": shape.Area(),
            "face_count": len(shape.Faces()),
            "edge_count": len(shape.Edges()),
            "vertex_count": len(shape.Vertices()),
        }
        return _ok(json.dumps(info, indent=2))

    @tool(
        "set_parameter",
        "Define or update a named parameter for the active artifact (object OR sketch). The script reads params via `params.get('name', default)`. The user can later tweak parameters in the Tweaks panel without rerunning the agent. Each artifact has its own params namespace.",
        {"name": str, "value": float},
    )
    async def set_parameter(args):
        proj = toolset.project
        kind, target = proj.active_artifact()
        if kind == "sketch":
            params = proj.read_sketch_params(target)
            params[args["name"]] = float(args["value"])
            proj.write_sketch_params(target, params)
        else:
            params = proj.read_object_params(target)
            params[args["name"]] = float(args["value"])
            proj.write_object_params(target, params)
        toolset.invalidate()
        return _ok(f"set {kind} {target}.{args['name']} = {args['value']}")

    @tool(
        "list_parameters",
        "Return the current parameters of the active artifact (object or sketch).",
        {},
    )
    async def list_parameters(args):
        proj = toolset.project
        kind, target = proj.active_artifact()
        if kind == "sketch":
            return _ok(json.dumps(proj.read_sketch_params(target), indent=2))
        return _ok(json.dumps(proj.read_object_params(target), indent=2))

    # ===== topology queries ===========================================

    @tool(
        "query_faces",
        "Apply a CADQuery face selector to the active object and return one entry per matching face: index, centroid, area, normal, geometry type. "
        "Common selectors: '>Z' top face, '<Z' bottom, '>X|<X|>Y|<Y' lateral, '%PLANE' planar, '%CYLINDER' cylindrical. "
        "Pass selector='all' to list every face.",
        {"selector": str},
    )
    async def query_faces(args):
        sel = args.get("selector") or "all"
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        faces = shape.Faces() if sel == "all" else model.faces(sel).vals()
        out = []
        for i, f in enumerate(faces):
            try:
                centroid = f.Center()
                normal = f.normalAt(centroid) if hasattr(f, "normalAt") else None
                out.append({
                    "index": i,
                    "type": f.geomType(),
                    "centroid": [centroid.x, centroid.y, centroid.z],
                    "normal": [normal.x, normal.y, normal.z] if normal else None,
                    "area": f.Area(),
                })
            except Exception as e:
                out.append({"index": i, "error": str(e)})
        return _ok(json.dumps(out, indent=2))

    @tool(
        "query_edges",
        "Apply a CADQuery edge selector to the active object and return per-edge info: index, geomType (LINE / CIRCLE / SPLINE / ...), length (mm), start / end points. "
        "Common selectors: '%CIRCLE' circular, '%LINE' straight, '|Z' parallel-to-Z, 'all' for every edge.",
        {"selector": str},
    )
    async def query_edges(args):
        sel = args.get("selector") or "all"
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        edges = shape.Edges() if sel == "all" else model.edges(sel).vals()
        out = []
        for i, e in enumerate(edges):
            try:
                row: dict[str, Any] = {
                    "index": i,
                    "type": e.geomType(),
                    "length": e.Length(),
                }
                try:
                    sp = e.startPoint()
                    ep = e.endPoint()
                    row["start"] = [sp.x, sp.y, sp.z]
                    row["end"] = [ep.x, ep.y, ep.z]
                except Exception:
                    # Closed edges (e.g. full circles) have no endpoints — fall
                    # back to a midpoint sample.
                    try:
                        mid = e.positionAt(0.5)
                        row["midpoint"] = [mid.x, mid.y, mid.z]
                    except Exception:
                        pass
                out.append(row)
            except Exception as ex:
                out.append({"index": i, "error": str(ex)})
        return _ok(json.dumps(out, indent=2))

    @tool(
        "query_vertices",
        "Return every vertex of the active object: index and (x, y, z) position in mm. Useful for pin-by-coordinate lookups and sanity-checking corner positions.",
        {},
    )
    async def query_vertices(args):
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        out = []
        for i, v in enumerate(shape.Vertices()):
            try:
                p = v.Center()
                out.append({"index": i, "point": [p.x, p.y, p.z]})
            except Exception as ex:
                out.append({"index": i, "error": str(ex)})
        return _ok(json.dumps(out, indent=2))

    # ===== analysis: validity / mass / distance =======================

    @tool(
        "check_validity",
        "Run an OCCT topology validity check on the active object. Returns whether the shape is geometrically valid (closed, well-formed). When invalid, returns a short reason. Use this whenever a boolean op or fillet might have produced a degenerate shape.",
        {},
    )
    async def check_validity(args):
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        try:
            from OCP.BRepCheck import BRepCheck_Analyzer
        except ImportError as e:
            return _err(f"OCCT BRepCheck not available: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        analyzer = BRepCheck_Analyzer(shape.wrapped)
        ok = bool(analyzer.IsValid())
        info: dict[str, Any] = {"valid": ok}
        if not ok:
            # Walk the sub-shapes to point at what's broken.
            broken = []
            for i, f in enumerate(shape.Faces()):
                if not BRepCheck_Analyzer(f.wrapped).IsValid():
                    broken.append({"face_index": i})
            for i, e in enumerate(shape.Edges()):
                if not BRepCheck_Analyzer(e.wrapped).IsValid():
                    broken.append({"edge_index": i})
            info["invalid_subshapes"] = broken
        return _ok(json.dumps(info, indent=2))

    @tool(
        "mass_properties",
        "Center of mass, volume, and inertia tensor of the active object (uniform unit density). The inertia tensor is reported as a 3x3 matrix about the centre of mass, in mm^5. Use this for balance / stability checks or comparing two design variants.",
        {},
    )
    async def mass_properties(args):
        try:
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        try:
            from OCP.BRepGProp import BRepGProp
            from OCP.GProp import GProp_GProps
        except ImportError as e:
            return _err(f"OCCT GProp not available: {e}")
        shape = model.val() if hasattr(model, "val") and callable(model.val) else model
        gp = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape.wrapped, gp)
        com = gp.CentreOfMass()
        mat = gp.MatrixOfInertia()
        info = {
            "volume_mm3": gp.Mass(),
            "center_of_mass": [com.X(), com.Y(), com.Z()],
            "inertia_about_com_mm5": [
                [mat.Value(1, 1), mat.Value(1, 2), mat.Value(1, 3)],
                [mat.Value(2, 1), mat.Value(2, 2), mat.Value(2, 3)],
                [mat.Value(3, 1), mat.Value(3, 2), mat.Value(3, 3)],
            ],
        }
        return _ok(json.dumps(info, indent=2))

    @tool(
        "distance_between",
        "Minimum distance (mm) between two entities. Each entity is referenced by string. "
        "Refs: 'name' for the whole object, 'name.face[i]' / 'name.edge[i]' / 'name.vertex[i]' for a sub-entity. "
        "If you omit the name (e.g. '.face[3]'), the active object is assumed. The two refs may belong to the same object — that's how you measure the gap between two features of one part. "
        "Returns 0 when the entities touch or intersect, plus the closest pair of world-coordinate points.",
        {"a": str, "b": str},
    )
    async def distance_between(args):
        try:
            sa = toolset.resolve_entity_ref(args["a"])
            sb = toolset.resolve_entity_ref(args["b"])
        except (ValueError, FileNotFoundError) as e:
            return _err(str(e))
        try:
            from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        except ImportError as e:
            return _err(f"OCCT BRepExtrema not available: {e}")
        ext = BRepExtrema_DistShapeShape(sa.wrapped, sb.wrapped)
        ext.Perform()
        if not ext.IsDone():
            return _err("distance computation did not converge")
        d = ext.Value()
        info: dict[str, Any] = {
            "a": args["a"], "b": args["b"],
            "distance_mm": d,
        }
        try:
            if ext.NbSolution() >= 1:
                p1 = ext.PointOnShape1(1)
                p2 = ext.PointOnShape2(1)
                info["closest_point_on_a"] = [p1.X(), p1.Y(), p1.Z()]
                info["closest_point_on_b"] = [p2.X(), p2.Y(), p2.Z()]
        except Exception:
            pass
        return _ok(json.dumps(info, indent=2))

    # ===== visual: section / scene / boolean preview ==================

    @tool(
        "section_snapshot",
        "Render a cross-section of the active object: the part is cut by an axis-aligned plane and the half you keep is shown so you can see internal features (pockets, holes, walls, ribs). "
        "axis: 'X' | 'Y' | 'Z' (the plane normal). offset: signed distance from origin along that axis. side: 'above' (default — keeps material on the lower side) or 'below'. view: 'iso' | 'front' | 'top' | etc., or pass camera={'position','target','up'} for an explicit pose.",
        {"axis": str, "offset": float, "side": str, "view": str, "camera": dict},
    )
    async def section_snapshot(args):
        proj = toolset.project
        active = proj.active_object()
        axis = (args.get("axis") or "Z").upper()
        offset = float(args.get("offset", 0.0))
        side = (args.get("side") or "above").lower()
        try:
            view_dict = _build_view_arg(args.get("view"), args.get("camera"))
        except ValueError as e:
            return _err(str(e))
        spec = {
            "items": [{
                "name": active,
                "script": str(proj.object_source_path(active)),
                "params": str(proj.object_params_path(active)),
            }],
            "post": {"kind": "section", "axis": axis, "offset": offset, "side": side},
            "view": view_dict,
            "width": 900, "height": 700,
        }
        result = scene_script(spec, cwd=proj.path, sketches=_sketches_manifest(proj))
        if not result.ok:
            msg = result.error or "section render failed"
            if result.stderr:
                msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
            return _err(msg)
        label = view_dict.get("preset") or "custom camera"
        text = (
            f"section of '{active}' along {axis} = {offset}mm "
            f"(removed {side}), view '{label}':"
        )
        return {"content": [{"type": "text", "text": text},
                            _image_block(result.png_bytes or b"")]}

    @tool(
        "scene_snapshot",
        "Render two or more objects together in one frame, each in a different colour. Use this to verify how parts sit next to each other (lid-on-case, screw-in-hole, etc.). Pass object names from this project. view: 'iso' | 'front' | 'top' | etc., or pass camera={'position','target','up'} for an explicit pose (matches a user screenshot).",
        {"objects": list, "view": str, "camera": dict},
    )
    async def scene_snapshot(args):
        proj = toolset.project
        names = args.get("objects") or []
        if not isinstance(names, list) or not names:
            return _err("'objects' must be a non-empty list of object names")
        try:
            view_dict = _build_view_arg(args.get("view"), args.get("camera"))
        except ValueError as e:
            return _err(str(e))
        items = []
        for n in names:
            if not proj.object_exists(n):
                return _err(f"object {n!r} does not exist (have: {[o['name'] for o in proj.list_objects()]})")
            items.append({
                "name": n,
                "script": str(proj.object_source_path(n)),
                "params": str(proj.object_params_path(n)),
            })
        spec = {
            "items": items,
            "post": None,
            "view": view_dict,
            "width": 900, "height": 700,
        }
        result = scene_script(spec, cwd=proj.path, sketches=_sketches_manifest(proj))
        if not result.ok:
            msg = result.error or "scene render failed"
            if result.stderr:
                msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
            return _err(msg)
        label = view_dict.get("preset") or "custom camera"
        return {"content": [
            {"type": "text", "text": f"scene of {names!r}, view '{label}':"},
            _image_block(result.png_bytes or b""),
        ]}

    @tool(
        "preview_boolean",
        "Compute and render the union / intersection / difference of two objects WITHOUT modifying any script. Use to check fit & clearance: an empty intersection means the parts don't overlap; a non-empty intersection shows exactly where they collide. "
        "op: 'union' | 'intersection' | 'difference' (a minus b). view: 'iso' | 'front' | 'top' | etc., or pass camera={'position','target','up'} for an explicit pose.",
        {"a": str, "b": str, "op": str, "view": str, "camera": dict},
    )
    async def preview_boolean(args):
        proj = toolset.project
        a, b = args.get("a"), args.get("b")
        op = (args.get("op") or "union").lower()
        try:
            view_dict = _build_view_arg(args.get("view"), args.get("camera"))
        except ValueError as e:
            return _err(str(e))
        if not a or not b:
            return _err("'a' and 'b' (object names) are required")
        for n in (a, b):
            if not proj.object_exists(n):
                return _err(f"object {n!r} does not exist")
        spec = {
            "items": [
                {"name": a,
                 "script": str(proj.object_source_path(a)),
                 "params": str(proj.object_params_path(a))},
                {"name": b,
                 "script": str(proj.object_source_path(b)),
                 "params": str(proj.object_params_path(b))},
            ],
            "post": {"kind": "boolean", "op": op, "a": a, "b": b},
            "view": view_dict,
            "width": 900, "height": 700,
        }
        result = scene_script(spec, cwd=proj.path, sketches=_sketches_manifest(proj))
        if not result.ok:
            msg = result.error or "boolean preview failed"
            if result.stderr:
                msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
            return _err(msg)
        label = view_dict.get("preset") or "custom camera"
        text = (
            f"{op} of '{a}' and '{b}' (transient — neither script was modified), "
            f"view '{label}':"
        )
        return {"content": [{"type": "text", "text": text},
                            _image_block(result.png_bytes or b"")]}

    # ===== expression escape hatch =====================================

    @tool(
        "eval_expression",
        "Evaluate a Python expression with the active artifact in scope. "
        "For an active OBJECT: `model` (cq.Workplane), `cq`, `params`, `sketches` (dict of placed cq.Workplane). "
        "For an active SKETCH: `sketch_wp` (cq.Workplane with the sketch placed), `sketch` (raw cq.Sketch), `cq`, `params`. "
        "Use for ad-hoc inspection — e.g. \"model.faces('>Z').val().Area()\", \"sketch_wp.val().Edges()[0].Length()\". The expression must be a single expression, not statements.",
        {"expression": str},
    )
    async def eval_expression(args):
        expr = args["expression"]
        proj = toolset.project
        kind, name = proj.active_artifact()
        try:
            import cadquery as cq
            if kind == "sketch":
                wp = toolset.load_sketch_in_process(name)
                # Pull the raw cq.Sketch back out for direct introspection.
                raw_sketch = None
                try:
                    if getattr(wp, "objects", None):
                        for obj in wp.objects:
                            if isinstance(obj, cq.Sketch):
                                raw_sketch = obj
                                break
                except Exception:
                    pass
                ns = {
                    "sketch_wp": wp,
                    "sketch": raw_sketch,
                    "cq": cq,
                    "params": proj.read_sketch_params(name),
                }
            else:
                model = toolset.load_object_in_process(name)
                ns = {
                    "model": model,
                    "cq": cq,
                    "params": proj.read_object_params(name),
                    "sketches": toolset._sketches_dict_in_process(),
                }
        except Exception as e:
            return _err(f"could not load active artifact: {e}")
        try:
            value = eval(expr, ns)
        except Exception as e:
            return _err(f"expression error: {e}\n\n{traceback.format_exc()}")
        return _ok(repr(value))

    # ===== object management ==========================================

    @tool(
        "list_objects",
        "List every object in this project. Returns each object's name and which one is currently active. The agent edits whichever object is active; use set_active_object to switch.",
        {},
    )
    async def list_objects(args):
        proj = toolset.project
        return _ok(json.dumps({
            "active": proj.active_object(),
            "objects": [o["name"] for o in proj.list_objects()],
        }, indent=2))

    @tool(
        "create_object",
        "Create a new object in this project (a separate CADQuery script with its own params). The new object becomes active immediately, so subsequent run_model / Edit calls operate on it. Use this when the user asks for a *new* part rather than a change to the existing one.",
        {"name": str},
    )
    async def create_object(args):
        proj = toolset.project
        try:
            safe = proj.create_object(args["name"])
        except (ValueError, FileExistsError) as e:
            return _err(str(e))
        toolset.invalidate()
        result = run_script(
            proj.object_source_path(safe),
            proj.object_params_path(safe),
            cwd=proj.path,
            timeout=30.0,
            sketches=_sketches_manifest(proj),
        )
        toolset.render(result)
        return _ok(
            f"created object '{safe}' and made it active. The seed script is "
            f"at objects/{safe}.py — read and edit it next. Remember: prefer "
            f"to drive new geometry from a fully-constrained sketch via "
            f"create_sketch + sketches['name'].extrude(...) rather than "
            f"inlining the 2D profile here."
        )

    @tool(
        "set_active_object",
        "Switch which object is currently active. Selecting an object also flips the edit target back to 'object' (so subsequent Read/Edit/Write hit the object's script, not whatever sketch was last open). The viewer renders every *visible* object; switching active does not change visibility.",
        {"name": str},
    )
    async def set_active_object(args):
        proj = toolset.project
        try:
            proj.set_active_object(args["name"])
        except FileNotFoundError as e:
            return _err(str(e))
        toolset.invalidate()
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return _ok(f"active object is now '{args['name']}'")

    # ===== sketch management ==========================================

    @tool(
        "list_sketches",
        "List every sketch in this project. Returns each sketch's name, whether it's currently visible in the viewer overlay, and which sketch is active (if any).",
        {},
    )
    async def list_sketches(args):
        proj = toolset.project
        return _ok(json.dumps({
            "active_kind": proj.active_kind(),
            "active_sketch": proj.active_sketch(),
            "sketches": [
                {"name": s["name"], "visible": s.get("visible", True)}
                for s in proj.list_sketches()
            ],
        }, indent=2))

    @tool(
        "create_sketch",
        "Create a new fully-constrained 2D sketch (a separate CADQuery script defining `sketch` and optionally `plane`). The new sketch becomes the active artifact, so subsequent Read / Edit / run_model calls work on it. Object scripts can consume sketches via the injected `sketches` dict — e.g. `sketches['profile'].extrude(20)`.",
        {"name": str},
    )
    async def create_sketch(args):
        proj = toolset.project
        try:
            safe = proj.create_sketch(args["name"])
        except (ValueError, FileExistsError) as e:
            return _err(str(e))
        toolset.invalidate()
        # Tessellate the seed sketch so the viewer overlay shows it.
        sk = tessellate_sketch_script(
            proj.sketch_source_path(safe),
            proj.sketch_params_path(safe),
            cwd=proj.path,
            timeout=20.0,
        )
        bus.emit("doc_sketch_geometry", {
            "doc_id": proj.id, "sketch": safe,
            "ok": sk.ok, "error": sk.error, "stderr": sk.stderr,
            "plane": sk.plane, "polylines": sk.polylines, "bbox": sk.bbox,
        })
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return _ok(
            f"created sketch '{safe}' and made it active. The seed script is "
            f"at sketches/{safe}.py — read and edit it next. Remember: every "
            f"dimension must be explicit (numeric or via params); use "
            f".constrain(...).solve() if you need geometric constraints "
            f"(coincident, parallel, perpendicular, distance, etc.)."
        )

    @tool(
        "set_active_sketch",
        "Switch the active artifact to a sketch. Read/Edit/Write and run_model now target this sketch. Use this before editing a sketch the user asked you to modify.",
        {"name": str},
    )
    async def set_active_sketch(args):
        proj = toolset.project
        try:
            proj.set_active_sketch(args["name"])
        except FileNotFoundError as e:
            return _err(str(e))
        toolset.invalidate()
        bus.emit("project_state", {"doc_id": proj.id, "state": proj.to_json()})
        return _ok(f"active sketch is now '{args['name']}' (edit target switched)")

    @tool(
        "snapshot_sketch",
        "Render a PNG of one sketch on its plane — useful for verifying a fully-constrained profile before extruding from it. Pass a name or omit to use the active sketch. view: 'iso' | 'top' | 'front' | etc., or pass camera={'position','target','up'}.",
        {"name": str, "view": str, "camera": dict},
    )
    async def snapshot_sketch(args):
        proj = toolset.project
        name = args.get("name") or proj.active_sketch()
        if not name:
            return _err("no sketch specified and no active sketch")
        if not proj.sketch_exists(name):
            return _err(f"sketch {name!r} does not exist")
        try:
            view_dict = _build_view_arg(args.get("view"), args.get("camera"))
        except ValueError as e:
            return _err(str(e))
        # Snapshot's worker expects an object script — wrap the sketch in a
        # tiny model script that turns the placed sketch into a degenerate
        # 3D shape (faces only, zero thickness). Easiest path: write a temp
        # "viewer" script that imports the real sketch script and exposes
        # sketches' faces as a `model`.
        # Simpler: render via the snapshot tool by extruding the sketch by
        # a thin slab so VTK has surface area to render.
        import tempfile as _tmp
        import textwrap as _tw
        viewer_dir = proj.path / ".agentcad-cache"
        viewer_dir.mkdir(exist_ok=True)
        viewer_path = viewer_dir / f"_sketch_view_{name}.py"
        viewer_path.write_text(_tw.dedent(f'''
            """Auto-generated transient view for sketch {name!r}."""
            model = sketches[{name!r}].extrude(0.05)
        ''').strip(), encoding="utf-8")
        params_path = proj.sketch_params_path(name)
        result = snapshot_script(
            viewer_path,
            params_path,
            view_dict,
            cwd=proj.path,
            width=900, height=700, timeout=30.0,
            sketches=_sketches_manifest(proj),
        )
        try:
            viewer_path.unlink()
        except OSError:
            pass
        if not result.ok:
            msg = result.error or "snapshot_sketch failed"
            if result.stderr:
                msg += "\n\nstderr:\n" + result.stderr.strip()[-1500:]
            return _err(msg)
        label = view_dict.get("preset") or "custom camera"
        return {
            "content": [
                {"type": "text", "text": f"sketch '{name}' on its plane, view '{label}':"},
                _image_block(result.png_bytes or b""),
            ]
        }

    # ===== git / timeline =============================================

    @tool(
        "git_log",
        "Return the most recent commits in this project's history (the timeline). Each entry is sha/short/subject/body/date.",
        {"limit": int},
    )
    async def git_log(args):
        n = int(args.get("limit") or 20)
        commits = toolset.project.log(limit=n)
        return _ok(json.dumps([c.to_json() for c in commits], indent=2))

    @tool(
        "commit_turn",
        "Commit the current working tree (all objects + assets) as one timeline entry. Call this at the end of a turn once the model is in a state worth saving. Use a short imperative subject like 'add chamfer to bottom face'.",
        {"subject": str, "body": str},
    )
    async def commit_turn(args):
        if not toolset.project.has_uncommitted():
            return _ok("nothing to commit (working tree clean)")
        sha = toolset.project.commit(args["subject"], args.get("body", ""))
        return _ok(f"committed {sha[:8]} — {args['subject']}")

    return [
        run_model, snapshot, measure, set_parameter, list_parameters,
        query_faces, query_edges, query_vertices,
        check_validity, mass_properties, distance_between,
        section_snapshot, scene_snapshot, preview_boolean,
        eval_expression,
        list_objects, create_object, set_active_object,
        list_sketches, create_sketch, set_active_sketch, snapshot_sketch,
        git_log, commit_turn,
    ]


# Built-in SDK tools we expose alongside the CAD tools. Bash is intentionally
# omitted; the agent shouldn't be running shell commands on the user's box.
BUILTIN_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]

CAD_TOOL_NAMES = [
    "mcp__cad__run_model",
    "mcp__cad__snapshot",
    "mcp__cad__measure",
    "mcp__cad__set_parameter",
    "mcp__cad__list_parameters",
    "mcp__cad__query_faces",
    "mcp__cad__query_edges",
    "mcp__cad__query_vertices",
    "mcp__cad__check_validity",
    "mcp__cad__mass_properties",
    "mcp__cad__distance_between",
    "mcp__cad__section_snapshot",
    "mcp__cad__scene_snapshot",
    "mcp__cad__preview_boolean",
    "mcp__cad__eval_expression",
    "mcp__cad__list_objects",
    "mcp__cad__create_object",
    "mcp__cad__set_active_object",
    "mcp__cad__list_sketches",
    "mcp__cad__create_sketch",
    "mcp__cad__set_active_sketch",
    "mcp__cad__snapshot_sketch",
    "mcp__cad__git_log",
    "mcp__cad__commit_turn",
]

ALL_TOOL_NAMES = BUILTIN_TOOLS + CAD_TOOL_NAMES
