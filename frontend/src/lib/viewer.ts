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

export type ViewerCtx = {
  // Each visible object's geometry, in display order.
  visible: VisibleObject[];
  // The active object's name — picking, pinning, and the error banner are scoped to it.
  activeName: string | null;
  // Active object's error, if any (surfaced as the banner).
  errorMsg: string | null;
};

export const ViewerContext = createContext<ViewerCtx>({
  visible: [],
  activeName: null,
  errorMsg: null,
});

export const useViewer = () => useContext(ViewerContext);
