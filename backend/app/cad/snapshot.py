"""Offscreen VTK renderer — produces PNGs of CADQuery shapes from a
specified viewpoint. Used by the agent's snapshot / scene tools so
multimodal Claude can see what it's making.
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import vtk
from vtk.util import numpy_support

# CADQuery convention: +Z is up.
PRESETS: dict[str, dict[str, list[float]]] = {
    "iso":    {"dir": [1, -1, 0.8], "up": [0, 0, 1]},
    "front":  {"dir": [0, -1, 0],   "up": [0, 0, 1]},
    "back":   {"dir": [0, 1, 0],    "up": [0, 0, 1]},
    "right":  {"dir": [1, 0, 0],    "up": [0, 0, 1]},
    "left":   {"dir": [-1, 0, 0],   "up": [0, 0, 1]},
    "top":    {"dir": [0, 0, 1],    "up": [0, 1, 0]},
    "bottom": {"dir": [0, 0, -1],   "up": [0, 1, 0]},
}

# A small palette for distinguishing objects when rendering a scene.
SCENE_COLORS: list[tuple[float, float, float]] = [
    (0.78, 0.81, 0.86),  # cool grey  (default body)
    (0.86, 0.62, 0.40),  # warm tan
    (0.55, 0.78, 0.86),  # cyan-grey
    (0.86, 0.78, 0.55),  # warm yellow
    (0.78, 0.55, 0.86),  # purple
    (0.55, 0.86, 0.62),  # green
]


def _shape_of(workplane: Any):
    return workplane.val() if hasattr(workplane, "val") and callable(workplane.val) else workplane


def _build_polydata(shape, deflection: float = 0.1) -> vtk.vtkPolyData:
    points = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    base = 0
    for face in shape.Faces():
        verts, tris = face.tessellate(deflection)
        if not verts or not tris:
            continue
        for v in verts:
            points.InsertNextPoint(v.x, v.y, v.z)
        for t in tris:
            tri = vtk.vtkTriangle()
            tri.GetPointIds().SetId(0, base + t[0])
            tri.GetPointIds().SetId(1, base + t[1])
            tri.GetPointIds().SetId(2, base + t[2])
            cells.InsertNextCell(tri)
        base += len(verts)
    if base == 0:
        verts, tris = shape.tessellate(deflection)
        for v in verts:
            points.InsertNextPoint(v.x, v.y, v.z)
        for t in tris:
            tri = vtk.vtkTriangle()
            tri.GetPointIds().SetId(0, t[0])
            tri.GetPointIds().SetId(1, t[1])
            tri.GetPointIds().SetId(2, t[2])
            cells.InsertNextCell(tri)
    poly = vtk.vtkPolyData()
    poly.SetPoints(points)
    poly.SetPolys(cells)
    return poly


def _build_actors(poly: vtk.vtkPolyData,
                  body_color: tuple[float, float, float],
                  opacity: float) -> tuple[vtk.vtkActor, vtk.vtkActor]:
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(poly)
    normals.SetFeatureAngle(30.0)
    normals.SplittingOn()
    normals.Update()

    mapper = vtk.vtkPolyDataMapper()
    mapper.SetInputConnection(normals.GetOutputPort())

    actor = vtk.vtkActor()
    actor.SetMapper(mapper)
    p = actor.GetProperty()
    p.SetColor(*body_color)
    p.SetAmbient(0.12)
    p.SetDiffuse(0.55)
    p.SetSpecular(0.18)
    p.SetSpecularPower(25)
    p.SetOpacity(opacity)

    edges = vtk.vtkFeatureEdges()
    edges.SetInputData(poly)
    edges.BoundaryEdgesOff()
    edges.NonManifoldEdgesOff()
    edges.ManifoldEdgesOff()
    edges.FeatureEdgesOn()
    edges.SetFeatureAngle(20.0)
    edge_mapper = vtk.vtkPolyDataMapper()
    edge_mapper.SetInputConnection(edges.GetOutputPort())
    edge_actor = vtk.vtkActor()
    edge_actor.SetMapper(edge_mapper)
    edge_actor.GetProperty().SetColor(0.05, 0.05, 0.06)
    edge_actor.GetProperty().SetLineWidth(1.2)
    edge_actor.GetProperty().SetOpacity(min(1.0, opacity + 0.2))

    return actor, edge_actor


def _apply_view(renderer: vtk.vtkRenderer, bbox_min, bbox_max,
                view: dict | None, fov_deg: float) -> None:
    cx = 0.5 * (bbox_min[0] + bbox_max[0])
    cy = 0.5 * (bbox_min[1] + bbox_max[1])
    cz = 0.5 * (bbox_min[2] + bbox_max[2])
    diag = math.dist(bbox_min, bbox_max) or 1.0

    cam = renderer.GetActiveCamera()
    cam.SetViewAngle(fov_deg)

    if view and view.get("position") and view.get("target"):
        cam.SetPosition(*view["position"])
        cam.SetFocalPoint(*view["target"])
        cam.SetViewUp(*(view.get("up") or [0, 0, 1]))
        renderer.ResetCameraClippingRange()
        return

    spec = PRESETS.get((view or {}).get("preset", "iso"), PRESETS["iso"])
    direction = np.array(spec["dir"], dtype=float)
    direction /= np.linalg.norm(direction) or 1.0

    cam.SetFocalPoint(cx, cy, cz)
    cam.SetPosition(*(np.array([cx, cy, cz]) + direction * diag))
    cam.SetViewUp(*spec["up"])
    renderer.ResetCamera(bbox_min[0], bbox_max[0],
                         bbox_min[1], bbox_max[1],
                         bbox_min[2], bbox_max[2])
    renderer.ResetCameraClippingRange()


def _union_bbox(shapes: Iterable[Any]) -> tuple[list[float], list[float]]:
    xmin = ymin = zmin = float("inf")
    xmax = ymax = zmax = float("-inf")
    found = False
    for shape in shapes:
        bb = shape.BoundingBox()
        xmin = min(xmin, bb.xmin); ymin = min(ymin, bb.ymin); zmin = min(zmin, bb.zmin)
        xmax = max(xmax, bb.xmax); ymax = max(ymax, bb.ymax); zmax = max(zmax, bb.zmax)
        found = True
    if not found:
        return [-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]
    return [xmin, ymin, zmin], [xmax, ymax, zmax]


def render_scene(items: list[dict], view: dict | None = None, *,
                 width: int = 800, height: int = 600,
                 background: tuple[float, float, float] = (0.12, 0.12, 0.12),
                 deflection: float = 0.1, fov_deg: float = 30.0) -> bytes:
    """Render multiple shapes in one frame.

    items: list of {"shape": cadquery shape or workplane,
                    "color": (r,g,b),
                    "opacity": float in [0, 1]}.
    """
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*background)

    raw_shapes = []
    for item in items:
        shape = _shape_of(item["shape"])
        raw_shapes.append(shape)
        poly = _build_polydata(shape, deflection)
        body_actor, edge_actor = _build_actors(
            poly,
            tuple(item.get("color") or SCENE_COLORS[0]),
            float(item.get("opacity", 1.0)),
        )
        renderer.AddActor(body_actor)
        renderer.AddActor(edge_actor)

    light_kit = vtk.vtkLightKit()
    light_kit.SetKeyLightIntensity(0.9)
    light_kit.SetKeyToFillRatio(2.5)
    light_kit.SetKeyToHeadRatio(3.0)
    light_kit.SetKeyToBackRatio(3.5)
    light_kit.AddLightsToRenderer(renderer)

    bb_min, bb_max = _union_bbox(raw_shapes)
    _apply_view(renderer, bb_min, bb_max, view, fov_deg)

    rw = vtk.vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.SetSize(int(width), int(height))
    rw.AddRenderer(renderer)
    rw.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(rw)
    w2i.SetInputBufferTypeToRGB()
    w2i.ReadFrontBufferOff()
    w2i.Update()

    writer = vtk.vtkPNGWriter()
    writer.WriteToMemoryOn()
    writer.SetInputConnection(w2i.GetOutputPort())
    writer.Write()

    arr = writer.GetResult()
    np_arr = numpy_support.vtk_to_numpy(arr)
    return np_arr.tobytes()


def render_png(workplane: Any, view: dict | None = None, *,
               width: int = 800, height: int = 600,
               background: tuple[float, float, float] = (0.12, 0.12, 0.12),
               body_color: tuple[float, float, float] = (0.78, 0.81, 0.86),
               deflection: float = 0.1, fov_deg: float = 30.0) -> bytes:
    """Render a single workplane to an offscreen PNG."""
    return render_scene(
        [{"shape": workplane, "color": body_color, "opacity": 1.0}],
        view, width=width, height=height,
        background=background, deflection=deflection, fov_deg=fov_deg,
    )
