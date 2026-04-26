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
)
from ..events import bus


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
    don't change anything don't re-execute the script.
    """

    def __init__(self, project: Project, render: Callable[[RunResult], None]):
        self.project = project
        self.render = render
        self._model_cache: dict[str, tuple] = {}

    def load_object_in_process(self, name: str):
        """Run any object's script in this process and return its `model`."""
        script = self.project.object_source_path(name)
        if not script.exists():
            raise FileNotFoundError(f"object {name!r} does not exist")
        st = script.stat().st_mtime
        params = self.project.read_object_params(name)
        sig = (st, json.dumps(params, sort_keys=True))
        cached = self._model_cache.get(name)
        if cached and cached[0] == sig:
            return cached[1]
        globs = runpy.run_path(str(script), init_globals={"params": params})
        model = globs.get("model")
        if model is None:
            raise RuntimeError(f"{script.name} finished without defining `model`")
        self._model_cache[name] = (sig, model)
        return model

    def load_model_in_process(self):
        """Active object's model — convenience for tools that don't take an object name."""
        return self.load_object_in_process(self.project.active_object())

    def invalidate(self) -> None:
        self._model_cache.clear()

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
        "Execute the active object's script in a sandboxed subprocess. ALWAYS call this after editing the script to verify it parses and produces a valid Workplane. The new geometry is automatically pushed to the viewer. Returns ok/error and a brief meta summary (bbox, volume, face count). Errors include the Python traceback so you can fix them.",
        {},
    )
    async def run_model(args):
        toolset.invalidate()
        proj = toolset.project
        active = proj.active_object()
        result = run_script(
            proj.object_source_path(active),
            proj.object_params_path(active),
            cwd=proj.path,
            timeout=30.0,
        )
        toolset.render(result)
        if result.ok:
            m = result.meta or {}
            bb = m.get("bbox", {})
            summary = (
                f"OK ({active}). bbox size: {bb.get('size')}, "
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
        "Define or update a named parameter for the active object. The script reads params via `params.get('name', default)`. The user can later tweak parameters in the Tweaks panel without rerunning the agent.",
        {"name": str, "value": float},
    )
    async def set_parameter(args):
        proj = toolset.project
        active = proj.active_object()
        params = proj.read_object_params(active)
        params[args["name"]] = float(args["value"])
        proj.write_object_params(active, params)
        toolset.invalidate()
        return _ok(f"set {active}.{args['name']} = {args['value']}")

    @tool(
        "list_parameters",
        "Return the current parameters of the active object.",
        {},
    )
    async def list_parameters(args):
        proj = toolset.project
        return _ok(json.dumps(proj.read_object_params(proj.active_object()), indent=2))

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
        result = scene_script(spec, cwd=proj.path)
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
        result = scene_script(spec, cwd=proj.path)
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
        result = scene_script(spec, cwd=proj.path)
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
        "Evaluate a Python expression with `model` (cadquery.Workplane for the active object), `cq` (cadquery module), and `params` (dict) in scope. Use for ad-hoc measurements not covered by other tools — e.g. \"model.faces('>Z').val().Area()\" or \"model.val().BoundingBox().DiagonalLength\". The expression must be a single expression, not statements.",
        {"expression": str},
    )
    async def eval_expression(args):
        expr = args["expression"]
        try:
            import cadquery as cq
            model = toolset.load_model_in_process()
        except Exception as e:
            return _err(f"could not load model: {e}")
        try:
            value = eval(expr, {
                "model": model, "cq": cq,
                "params": toolset.project.read_object_params(toolset.project.active_object()),
            })
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
        )
        toolset.render(result)
        return _ok(f"created object '{safe}' and made it active. The seed script is at objects/{safe}.py — read and edit it next.")

    @tool(
        "set_active_object",
        "Switch which object is currently active. The Tweaks panel and all script-level tools (run_model, snapshot, measure, set_parameter) follow the active object. Note: the viewer renders every *visible* object; switching active does not change visibility.",
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
    "mcp__cad__git_log",
    "mcp__cad__commit_turn",
]

ALL_TOOL_NAMES = BUILTIN_TOOLS + CAD_TOOL_NAMES
