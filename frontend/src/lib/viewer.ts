/**
 * Per-tab viewer state — shared so the ViewerPane doesn't have to keep its
 * own glb cache, and switching tabs swaps geometry instantly.
 */
import { createContext, useContext } from "react";

export type FaceMeta = {
  index: number;
  type?: string | null;
  centroid?: [number, number, number];
};

export type EdgeMeta = {
  index: number;
  type?: string | null;
  points: [number, number, number][];
};

export type VertexMeta = {
  index: number;
  point: [number, number, number];
};

export type Topology = {
  faces: FaceMeta[];
  edges: EdgeMeta[];
  vertices: VertexMeta[];
};

export type ObjectGeometry = {
  glbB64: string | null;
  topology: Topology | null;
  errorMsg: string | null;
};

export type VisibleObject = {
  name: string;
  geometry: ObjectGeometry;
};

/** A sketch's wires projected into 3D, ready for line rendering. */
export type SketchGeometry = {
  /** ordered point lists; one entry per closed-or-open wire */
  polylines: { points: [number, number, number][]; closed: boolean }[] | null;
  /** info about the sketch's plane (origin + axes), for future overlay HUD */
  plane: {
    origin: [number, number, number];
    x_dir: [number, number, number];
    y_dir: [number, number, number];
    normal: [number, number, number];
  } | null;
  errorMsg: string | null;
};

export type VisibleSketch = {
  name: string;
  geometry: SketchGeometry;
};

/** A STEP import's tessellation (mesh + topology), same shape as objects so
 * the viewer can reuse the GLB rendering pipeline. */
export type ImportGeometry = ObjectGeometry;

export type VisibleImport = {
  name: string;
  geometry: ImportGeometry;
};

export type ViewerCtx = {
  // Each visible object's geometry, in display order.
  visible: VisibleObject[];
  // Each visible sketch's geometry, in display order.
  visibleSketches: VisibleSketch[];
  // Each visible import's geometry, in display order.
  visibleImports: VisibleImport[];
  // The active object's name — picking, pinning, and the error banner are scoped to it.
  activeName: string | null;
  // Active object's error, if any (surfaced as the banner).
  errorMsg: string | null;
};

export const ViewerContext = createContext<ViewerCtx>({
  visible: [],
  visibleSketches: [],
  visibleImports: [],
  activeName: null,
  errorMsg: null,
});

export const useViewer = () => useContext(ViewerContext);
