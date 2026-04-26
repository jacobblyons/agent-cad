import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, ThreeEvent, useFrame, useThree } from "@react-three/fiber";
import {
  ContactShadows,
  Environment,
  GizmoHelper,
  GizmoViewport,
  Grid,
  Html,
  OrbitControls,
} from "@react-three/drei";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";
import * as THREE from "three";
import {
  Box,
  Boxes,
  Camera,
  CircleDot,
  MessageSquarePlus,
  Spline,
  Square,
  X,
} from "lucide-react";

import { base64ToArrayBuffer, useDoc } from "@/lib/doc";
import { useChat, type EntityKind } from "@/lib/chat";
import {
  useViewer,
  type EdgeMeta,
  type SketchGeometry,
  type Topology,
  type VertexMeta,
} from "@/lib/viewer";
import { cn } from "@/lib/utils";
import { DrawingDialog } from "./DrawingDialog";

type ViewMode = "shaded" | "unshaded" | "wireframe";
type ToolMode = "orbit" | "annotate";

const VIEW_OPTIONS: { id: ViewMode; label: string; Icon: typeof Box }[] = [
  { id: "shaded", label: "Shaded", Icon: Box },
  { id: "unshaded", label: "Unshaded", Icon: Square },
  { id: "wireframe", label: "Wireframe", Icon: Boxes },
];

const ENTITY_OPTIONS: { id: EntityKind; label: string; Icon: typeof Box }[] = [
  { id: "face", label: "Face", Icon: Square },
  { id: "edge", label: "Edge", Icon: Spline },
  { id: "vertex", label: "Vertex", Icon: CircleDot },
];

const EDGE_COLOR_DARK = "#1a1d22";
const HOVER_COLOR = 0x007acc;
const PIN_COLOR = 0xf59e0b;

// Palette for per-object body / wireframe colors. Mirrors the backend's
// snapshot palette so the viewer and agent renders use the same hues.
const OBJECT_PALETTE = [
  "#c4cdd9",  // cool grey  (default)
  "#dba075",  // warm tan
  "#8cc8d8",  // cyan-grey
  "#dbc78c",  // warm yellow
  "#c48cdb",  // purple
  "#8cdb9b",  // green
];

function colorForIndex(i: number): string {
  return OBJECT_PALETTE[((i % OBJECT_PALETTE.length) + OBJECT_PALETTE.length) % OBJECT_PALETTE.length];
}

// Raycast tuning. Lines need a generous threshold or you can never click
// them (they're 1px wide on screen). Vertex spheres pick reliably via mesh
// raycast at any threshold.
const LINE_PICK_THRESHOLD = 4;

const NOOP_RAYCAST = () => {};

/** CADQuery (+Z up) → three.js (+Y up): rotation -PI/2 about X. */
function cqToThree(p: [number, number, number]): [number, number, number] {
  return [p[0], p[2], -p[1]];
}

function threeToCq(p: THREE.Vector3): [number, number, number] {
  return [p.x, -p.z, p.y];
}

function findEntity(
  o: THREE.Object3D | null,
): { kind: EntityKind; index: number } | null {
  let cur: THREE.Object3D | null = o;
  while (cur) {
    const m = cur.name?.match(/^(face|edge|vertex)_(\d+)$/);
    if (m) {
      return { kind: m[1] as EntityKind, index: parseInt(m[2], 10) };
    }
    cur = cur.parent;
  }
  return null;
}

// --- FACES (the trimesh-built glb) ---------------------------------------

function FacesGroup({
  glbB64,
  viewMode,
  bodyColor,
  active,         // true if face is the current pick kind in annotate mode
  hoveredFace,
  pinnedFace,
}: {
  glbB64: string | null;
  viewMode: ViewMode;
  bodyColor: string;
  active: boolean;
  hoveredFace: number | null;
  pinnedFace: number | null;
}) {
  const [root, setRoot] = useState<THREE.Group | null>(null);
  const defaultRaycasts = useRef(new Map<THREE.Mesh, THREE.Mesh["raycast"]>());

  useEffect(() => {
    if (!glbB64) {
      setRoot(null);
      return;
    }
    const loader = new GLTFLoader();
    let cancelled = false;
    loader.parse(
      base64ToArrayBuffer(glbB64),
      "",
      (gltf) => {
        if (cancelled) return;
        const r = new THREE.Group();
        gltf.scene.rotation.x = -Math.PI / 2;
        r.add(gltf.scene);
        setRoot(r);
      },
      (err) => console.error("glTF parse error", err),
    );
    return () => {
      cancelled = true;
    };
  }, [glbB64]);

  // Materials per view mode.
  useEffect(() => {
    if (!root) return;
    root.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const oldMat = mesh.material as THREE.Material | THREE.Material[];
      if (Array.isArray(oldMat)) oldMat.forEach((m) => m.dispose());
      else oldMat?.dispose();

      switch (viewMode) {
        case "shaded":
          mesh.material = new THREE.MeshStandardMaterial({
            color: bodyColor,
            metalness: 0.18,
            roughness: 0.42,
            envMapIntensity: 0.7,
          });
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          break;
        case "unshaded":
          mesh.material = new THREE.MeshBasicMaterial({ color: bodyColor });
          mesh.castShadow = false;
          mesh.receiveShadow = false;
          break;
        case "wireframe":
          mesh.material = new THREE.MeshBasicMaterial({ visible: false });
          mesh.castShadow = false;
          mesh.receiveShadow = false;
          break;
      }
    });
  }, [root, viewMode, bodyColor]);

  // Raycast gating — only meshes whose entity kind matches the active pick
  // kind should intercept clicks. Otherwise faces would always block edge
  // and vertex picking because they're "in front".
  useEffect(() => {
    if (!root) return;
    root.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      if (!defaultRaycasts.current.has(mesh)) {
        defaultRaycasts.current.set(mesh, mesh.raycast);
      }
      mesh.raycast = active
        ? (defaultRaycasts.current.get(mesh) ?? THREE.Mesh.prototype.raycast)
        : NOOP_RAYCAST;
    });
  }, [root, active]);

  // Hover / pin tinting.
  useEffect(() => {
    if (!root) return;
    root.traverse((obj) => {
      const mesh = obj as THREE.Mesh;
      if (!mesh.isMesh) return;
      const m = mesh.name?.match(/^face_(\d+)$/);
      if (!m) return;
      const idx = parseInt(m[1], 10);
      const mat = mesh.material as THREE.MeshStandardMaterial | THREE.MeshBasicMaterial;
      if (!("color" in mat)) return;
      if (idx === pinnedFace) mat.color.setHex(PIN_COLOR);
      else if (idx === hoveredFace) mat.color.setHex(HOVER_COLOR);
      else mat.color.set(bodyColor);
      mat.needsUpdate = true;
    });
  }, [root, hoveredFace, pinnedFace, viewMode, bodyColor]);

  if (!root) return null;
  return <primitive object={root} />;
}

// --- EDGES (one named LineSegments per CADQuery edge) -------------------

function EdgesGroup({
  topology,
  viewMode,
  wireColor,
  active,
  hoveredEdge,
  pinnedEdge,
}: {
  topology: Topology | null;
  viewMode: ViewMode;
  wireColor: string;
  active: boolean;
  hoveredEdge: number | null;
  pinnedEdge: number | null;
}) {
  const groupRef = useRef<THREE.Group>(null);
  const edges = topology?.edges ?? [];
  const baseColor = viewMode === "wireframe" ? wireColor : EDGE_COLOR_DARK;

  const edgeObjects = useMemo(
    () => edges.map((edge) => buildEdgeLine(edge, baseColor)),
    [edges, baseColor],
  );

  useEffect(() => {
    if (!groupRef.current) return;
    groupRef.current.children.forEach((child) => {
      const ls = child as THREE.LineSegments;
      ls.raycast = active ? THREE.LineSegments.prototype.raycast : NOOP_RAYCAST;
    });
  }, [active, edgeObjects]);

  useEffect(() => {
    if (!groupRef.current) return;
    groupRef.current.children.forEach((child) => {
      const ls = child as THREE.LineSegments;
      const m = ls.name?.match(/^edge_(\d+)$/);
      if (!m) return;
      const idx = parseInt(m[1], 10);
      const mat = ls.material as THREE.LineBasicMaterial;
      if (idx === pinnedEdge) mat.color.setHex(PIN_COLOR);
      else if (idx === hoveredEdge) mat.color.setHex(HOVER_COLOR);
      else mat.color.set(baseColor);
    });
  }, [hoveredEdge, pinnedEdge, baseColor, edgeObjects]);

  return (
    <group ref={groupRef}>
      {edgeObjects.map((obj) => (
        <primitive key={obj.name} object={obj} />
      ))}
    </group>
  );
}

function buildEdgeLine(edge: EdgeMeta, color: string): THREE.LineSegments {
  const positions: number[] = [];
  for (let i = 0; i < edge.points.length - 1; i++) {
    const a = cqToThree(edge.points[i]);
    const b = cqToThree(edge.points[i + 1]);
    positions.push(a[0], a[1], a[2], b[0], b[1], b[2]);
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  const mat = new THREE.LineBasicMaterial({ color, linewidth: 1 });
  const ls = new THREE.LineSegments(geom, mat);
  ls.name = `edge_${edge.index}`;
  ls.renderOrder = 1;
  return ls;
}

// --- VERTICES (small spheres, only visible in vertex sub-mode) ----------

function VerticesGroup({
  topology,
  active,
  hoveredVertex,
  pinnedVertex,
}: {
  topology: Topology | null;
  active: boolean;
  hoveredVertex: number | null;
  pinnedVertex: number | null;
}) {
  const vertices = topology?.vertices ?? [];
  if (!active || vertices.length === 0) return null;
  return (
    <group>
      {vertices.map((v) => (
        <Vertex
          key={v.index}
          vertex={v}
          hovered={hoveredVertex === v.index}
          pinned={pinnedVertex === v.index}
        />
      ))}
    </group>
  );
}

function Vertex({
  vertex,
  hovered,
  pinned,
}: {
  vertex: VertexMeta;
  hovered: boolean;
  pinned: boolean;
}) {
  const pos = cqToThree(vertex.point);
  const color = pinned ? PIN_COLOR : hovered ? HOVER_COLOR : 0xcfd5df;
  // Smaller default; bump slightly when interacting. depthTest off so they
  // stay visible even when occluded inside holes.
  const radius = pinned || hovered ? 0.7 : 0.45;
  return (
    <mesh name={`vertex_${vertex.index}`} position={pos} renderOrder={2}>
      <sphereGeometry args={[radius, 12, 8]} />
      <meshBasicMaterial color={color} depthTest={false} />
    </mesh>
  );
}

// --- Pending-input bubble at the click point -----------------------------

function PendingPin({
  point,
  kind,
  index,
  onSubmit,
  onCancel,
}: {
  point: [number, number, number];
  kind: EntityKind;
  index: number | null;
  onSubmit: (text: string) => void;
  onCancel: () => void;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    setTimeout(() => ref.current?.focus(), 50);
  }, []);
  return (
    <Html position={cqToThree(point)} center style={{ pointerEvents: "auto" }} zIndexRange={[60, 0]}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-[280px] rounded-md border border-[var(--color-focus)] bg-[var(--color-panel)] p-2 shadow-xl"
      >
        <div className="mb-1 flex items-center justify-between">
          <span className="font-mono text-[10px] text-[var(--color-muted)]">
            {kind} {index ?? "?"} · ({point.map((n) => n.toFixed(1)).join(", ")})
          </span>
          <button
            onClick={onCancel}
            className="rounded-sm p-0.5 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
          >
            <X size={11} />
          </button>
        </div>
        <textarea
          ref={ref}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              onSubmit(text);
            } else if (e.key === "Escape") {
              e.preventDefault();
              onCancel();
            }
          }}
          rows={2}
          placeholder={`What about this ${kind}?  (Enter to send · Esc to cancel)`}
          className="w-full resize-none bg-transparent text-xs text-[var(--color-text)] outline-none placeholder:text-[var(--color-muted)]"
        />
        <div className="mt-1 flex justify-end gap-1">
          <button
            onClick={onCancel}
            className="rounded-sm px-2 py-0.5 text-[10px] text-[var(--color-muted)] hover:bg-[var(--color-hover)]"
          >
            Cancel
          </button>
          <button
            onClick={() => onSubmit(text)}
            disabled={!text.trim()}
            className="rounded-sm bg-[var(--color-accent)] px-2 py-0.5 text-[10px] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
          >
            Send
          </button>
        </div>
      </div>
    </Html>
  );
}

function ViewerToolbar({
  view,
  onView,
  tool,
  onTool,
  pickKind,
  onPickKind,
  onSnapshot,
}: {
  view: ViewMode;
  onView: (m: ViewMode) => void;
  tool: ToolMode;
  onTool: (m: ToolMode) => void;
  pickKind: EntityKind;
  onPickKind: (k: EntityKind) => void;
  onSnapshot: () => void;
}) {
  return (
    <div className="absolute right-3 top-3 flex flex-col items-end gap-1.5">
      <div className="flex gap-0.5 rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)]/90 p-0.5 backdrop-blur">
        {VIEW_OPTIONS.map(({ id, label, Icon }) => (
          <button
            key={id}
            onClick={() => onView(id)}
            title={label}
            className={cn(
              "flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs",
              view === id
                ? "bg-[var(--color-selection)] text-[var(--color-text)]"
                : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
            )}
          >
            <Icon size={12} />
            <span>{label}</span>
          </button>
        ))}
        <div className="mx-0.5 my-1 w-px bg-[var(--color-border)]" />
        <button
          onClick={() => onTool(tool === "annotate" ? "orbit" : "annotate")}
          title="Click a face / edge / vertex to send a pinned message"
          className={cn(
            "flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs",
            tool === "annotate"
              ? "bg-[var(--color-selection)] text-[var(--color-text)]"
              : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
          )}
        >
          <MessageSquarePlus size={12} />
          <span>Point</span>
        </button>
        <button
          onClick={onSnapshot}
          title="Snapshot the current view to annotate and attach"
          className="flex h-7 items-center gap-1.5 rounded-sm px-2 text-xs text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
        >
          <Camera size={12} />
          <span>Snapshot</span>
        </button>
      </div>
      {tool === "annotate" && (
        <div className="flex gap-0.5 rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)]/90 p-0.5 backdrop-blur">
          {ENTITY_OPTIONS.map(({ id, label, Icon }) => (
            <button
              key={id}
              onClick={() => onPickKind(id)}
              title={`Pick ${label.toLowerCase()}s`}
              className={cn(
                "flex h-6 items-center gap-1.5 rounded-sm px-2 text-[11px]",
                pickKind === id
                  ? "bg-[var(--color-accent)] text-[var(--color-accent-fg)]"
                  : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
              )}
            >
              <Icon size={10} />
              <span>{label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// --- sketch overlays ----------------------------------------------------

const SKETCH_COLOR = "#7dd3fc";        // sky-blue for inactive sketches
const SKETCH_COLOR_ACTIVE = "#fbbf24"; // amber for the active edit target

/**
 * Render one polyline as a chain of line segments. Buffer is rebuilt
 * whenever the polyline payload changes; we accept (closed, points) as
 * inputs because a closed wire needs the last→first segment too.
 */
function SketchPolyline({
  polyline,
  color,
}: {
  polyline: { points: [number, number, number][]; closed: boolean };
  color: string;
}) {
  const positions = useMemo(() => {
    const pts = polyline.points;
    if (!pts || pts.length < 2) return new Float32Array(0);
    const segCount = polyline.closed ? pts.length : pts.length - 1;
    const buf = new Float32Array(segCount * 6);
    let off = 0;
    for (let i = 0; i < segCount; i++) {
      const a = pts[i];
      const b = pts[(i + 1) % pts.length];
      buf[off++] = a[0]; buf[off++] = a[1]; buf[off++] = a[2];
      buf[off++] = b[0]; buf[off++] = b[1]; buf[off++] = b[2];
    }
    return buf;
  }, [polyline]);

  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    return g;
  }, [positions]);

  const material = useMemo(
    () =>
      new THREE.LineBasicMaterial({
        color,
        depthTest: false,
        depthWrite: false,
        transparent: true,
        opacity: 0.95,
      }),
    [color],
  );

  // Dispose on unmount so we don't leak when sketches change.
  useEffect(() => {
    return () => {
      geometry.dispose();
      material.dispose();
    };
  }, [geometry, material]);

  if (positions.length === 0) return null;
  // `renderOrder` is bumped so depthTest=false lines paint after solids and
  // win the visual battle without flickering against shadow geometry.
  return (
    <primitive
      object={new THREE.LineSegments(geometry, material)}
      renderOrder={10}
    />
  );
}

function SketchOverlay({
  sketches,
  activeSketch,
}: {
  sketches: { name: string; geometry: SketchGeometry }[];
  activeSketch: string | null;
}) {
  // Single rotation matches the GLB's CQ→three rotation — points stay in CQ
  // coords inside this group.
  return (
    <group rotation={[-Math.PI / 2, 0, 0]}>
      {sketches.map((s) => {
        const polylines = s.geometry.polylines ?? [];
        if (polylines.length === 0) return null;
        const color = s.name === activeSketch ? SKETCH_COLOR_ACTIVE : SKETCH_COLOR;
        return (
          <group key={s.name}>
            {polylines.map((p, i) => (
              <SketchPolyline key={i} polyline={p} color={color} />
            ))}
          </group>
        );
      })}
    </group>
  );
}

function CursorTracker({ enabled }: { enabled: boolean }) {
  const { gl } = useThree();
  useEffect(() => {
    gl.domElement.style.cursor = enabled ? "crosshair" : "";
    return () => {
      gl.domElement.style.cursor = "";
    };
  }, [enabled, gl]);
  return null;
}

type CameraSnap = {
  /** Camera position in CADQuery coords (Z up), millimetres. */
  position: [number, number, number];
  /** OrbitControls target in CADQuery coords (Z up), millimetres. */
  target: [number, number, number];
  /** Camera up vector in CADQuery coords. */
  up: [number, number, number];
};

/** Mirrors live camera + orbit-target state into a ref readable from outside Canvas. */
function CameraTracker({ outRef }: { outRef: React.MutableRefObject<CameraSnap | null> }) {
  const { camera, controls } = useThree();
  useFrame(() => {
    const t = (controls as unknown as { target?: THREE.Vector3 } | null)?.target;
    outRef.current = {
      position: threeToCq(camera.position),
      target: t ? threeToCq(t) : [0, 0, 0],
      up: threeToCq(camera.up),
    };
  });
  return null;
}

function fmtArr(v: [number, number, number]): string {
  return `[${v[0].toFixed(2)}, ${v[1].toFixed(2)}, ${v[2].toFixed(2)}]`;
}

// --- Main pane -----------------------------------------------------------

export function ViewerPane() {
  const { doc } = useDoc();
  const { send, addAttachment } = useChat();
  const { visible, visibleSketches, activeName, errorMsg } = useViewer();
  const activeSketch = doc?.active_sketch ?? null;
  const wrapperRef = useRef<HTMLDivElement>(null);
  const cameraRef = useRef<CameraSnap | null>(null);
  const [snapshot, setSnapshot] = useState<
    { data: string; description: string } | null
  >(null);

  const [viewMode, setViewMode] = useState<ViewMode>(
    () => (localStorage.getItem("agentcad:viewMode") as ViewMode) || "shaded",
  );
  const [toolMode, setToolMode] = useState<ToolMode>("orbit");
  const [pickKind, setPickKind] = useState<EntityKind>("face");
  const [hovered, setHovered] = useState<{ kind: EntityKind; index: number } | null>(null);
  const [pending, setPending] = useState<{
    kind: EntityKind;
    index: number | null;
    type: string | null;
    point: [number, number, number];
  } | null>(null);

  useEffect(() => {
    localStorage.setItem("agentcad:viewMode", viewMode);
  }, [viewMode]);

  useEffect(() => {
    setToolMode("orbit");
    setPending(null);
    setHovered(null);
  }, [doc?.id]);

  useEffect(() => {
    setHovered(null);
  }, [pickKind, toolMode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (pending) setPending(null);
        else if (toolMode === "annotate") setToolMode("orbit");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, toolMode]);

  const activeGeometry = useMemo(
    () => (activeName ? visible.find((v) => v.name === activeName)?.geometry ?? null : null),
    [visible, activeName],
  );
  const activeTopology = activeGeometry?.topology ?? null;

  const lookupType = (kind: EntityKind, index: number | null): string | null => {
    if (!activeTopology || index == null) return null;
    if (kind === "face") return activeTopology.faces.find((x) => x.index === index)?.type ?? null;
    if (kind === "edge") return activeTopology.edges.find((x) => x.index === index)?.type ?? null;
    return null;
  };

  // Single click handler at the wrapping group. Walks intersections in
  // distance order and picks the first one whose entity matches the
  // active pickKind. This lets us handle clicks even when multiple kinds
  // are stacked along the same ray.
  const handleClick = (e: ThreeEvent<MouseEvent>) => {
    if (toolMode !== "annotate") return;
    for (const hit of e.intersections) {
      const ent = findEntity(hit.object);
      if (!ent || ent.kind !== pickKind) continue;
      e.stopPropagation();
      setPending({
        kind: ent.kind,
        index: ent.index,
        type: lookupType(ent.kind, ent.index),
        point: threeToCq(hit.point),
      });
      return;
    }
  };

  const handleHover = (e: ThreeEvent<PointerEvent>) => {
    if (toolMode !== "annotate") return;
    for (const hit of e.intersections) {
      const ent = findEntity(hit.object);
      if (!ent || ent.kind !== pickKind) continue;
      setHovered({ kind: ent.kind, index: ent.index });
      return;
    }
    setHovered(null);
  };

  const submitPending = async (text: string) => {
    const t = text.trim();
    if (!doc || !pending || !t) {
      setPending(null);
      return;
    }
    await send(t, {
      pin: {
        entity_kind: pending.kind,
        entity_index: pending.index,
        entity_type: pending.type,
        pin_world: pending.point,
      },
    });
    setPending(null);
    setToolMode("orbit");
  };

  const captureSnapshot = () => {
    // Canvas keeps its last frame thanks to preserveDrawingBuffer; toDataURL
    // works without forcing a re-render.
    const c = wrapperRef.current?.querySelector("canvas");
    if (!c) return;
    const cam = cameraRef.current;
    const description = cam
      ? `Camera (CADQuery coords, +Z up, mm): ` +
        `position=${fmtArr(cam.position)}, target=${fmtArr(cam.target)}, up=${fmtArr(cam.up)}. ` +
        `Pass these directly as the snapshot tool's "camera" argument to render ` +
        `from the same angle. View mode: ${viewMode}.`
      : `View mode: ${viewMode}.`;
    setSnapshot({ data: c.toDataURL("image/png"), description });
  };

  return (
    <div ref={wrapperRef} className="absolute inset-0">
      <Canvas
        shadows
        dpr={[1, 2]}
        camera={{ position: [60, 50, 80], fov: 35, near: 0.1, far: 5000 }}
        gl={{ antialias: true, preserveDrawingBuffer: true }}
        raycaster={{
          params: {
            Line: { threshold: LINE_PICK_THRESHOLD },
            Points: { threshold: 1 },
            Mesh: {},
            LOD: {},
            Sprite: {},
          },
        }}
      >
        <CursorTracker enabled={toolMode === "annotate"} />
        <CameraTracker outRef={cameraRef} />
        <color attach="background" args={["#1e1e1e"]} />

        <ambientLight intensity={viewMode === "shaded" ? 0.18 : 0.0} />
        <directionalLight
          position={[40, 80, 60]}
          intensity={viewMode === "shaded" ? 1.5 : 0.0}
          castShadow={viewMode === "shaded"}
          shadow-mapSize={[2048, 2048]}
          shadow-bias={-0.0005}
        />
        <directionalLight
          position={[-50, 40, -30]}
          intensity={viewMode === "shaded" ? 0.55 : 0.0}
        />
        <directionalLight
          position={[20, -40, -60]}
          intensity={viewMode === "shaded" ? 0.35 : 0.0}
        />
        <Suspense fallback={null}>
          {viewMode === "shaded" && (
            <Environment preset="city" environmentIntensity={0.5} />
          )}
        </Suspense>

        {/* All pickable scene under one event-handling group. Picking is
            scoped to the active object only; other visible objects render but
            don't intercept clicks. */}
        <group onClick={handleClick} onPointerMove={handleHover}>
          {visible.map((v, i) => {
            const isActive = v.name === activeName;
            const color = colorForIndex(i);
            return (
              <group key={v.name}>
                <FacesGroup
                  glbB64={v.geometry.glbB64}
                  viewMode={viewMode}
                  bodyColor={color}
                  active={isActive && toolMode === "annotate" && pickKind === "face"}
                  hoveredFace={isActive && hovered?.kind === "face" ? hovered.index : null}
                  pinnedFace={isActive && pending?.kind === "face" ? pending.index : null}
                />
                <EdgesGroup
                  topology={v.geometry.topology}
                  viewMode={viewMode}
                  wireColor={color}
                  active={isActive && toolMode === "annotate" && pickKind === "edge"}
                  hoveredEdge={isActive && hovered?.kind === "edge" ? hovered.index : null}
                  pinnedEdge={isActive && pending?.kind === "edge" ? pending.index : null}
                />
                {isActive && (
                  <VerticesGroup
                    topology={v.geometry.topology}
                    active={toolMode === "annotate" && pickKind === "vertex"}
                    hoveredVertex={hovered?.kind === "vertex" ? hovered.index : null}
                    pinnedVertex={pending?.kind === "vertex" ? pending.index : null}
                  />
                )}
              </group>
            );
          })}
        </group>

        {pending && (
          <PendingPin
            kind={pending.kind}
            index={pending.index}
            point={pending.point}
            onSubmit={submitPending}
            onCancel={() => setPending(null)}
          />
        )}

        <SketchOverlay sketches={visibleSketches} activeSketch={activeSketch} />

        {viewMode === "shaded" && (
          <ContactShadows
            position={[0, -0.01, 0]}
            opacity={0.45}
            scale={300}
            blur={2.4}
            far={120}
            resolution={1024}
            color="#000000"
          />
        )}

        <Grid
          args={[200, 200]}
          cellSize={5}
          cellThickness={0.6}
          cellColor="#2f3338"
          sectionSize={25}
          sectionThickness={1.0}
          sectionColor="#4a4f57"
          fadeDistance={250}
          fadeStrength={1}
          infiniteGrid
        />

        <OrbitControls makeDefault enableDamping dampingFactor={0.08} />
        <GizmoHelper alignment="bottom-right" margin={[64, 64]}>
          <GizmoViewport
            axisColors={["#f48771", "#89d185", "#75beff"]}
            labelColor="#1e1e1e"
          />
        </GizmoHelper>
      </Canvas>

      <ViewerToolbar
        view={viewMode}
        onView={setViewMode}
        tool={toolMode}
        onTool={setToolMode}
        pickKind={pickKind}
        onPickKind={setPickKind}
        onSnapshot={captureSnapshot}
      />

      <DrawingDialog
        open={snapshot !== null}
        onClose={() => setSnapshot(null)}
        onAttach={(img) =>
          addAttachment({
            ...img,
            source: "snapshot",
            description: snapshot?.description,
          })
        }
        background={snapshot?.data ?? null}
      />

      {toolMode === "annotate" && !pending && (
        <div className="pointer-events-none absolute left-1/2 top-3 -translate-x-1/2 rounded-md border border-[var(--color-focus)] bg-[var(--color-panel-2)]/95 px-3 py-1.5 text-xs text-[var(--color-text)] backdrop-blur">
          Click a <span className="font-medium">{pickKind}</span> to send a pinned message
        </div>
      )}

      {errorMsg && (
        <div className="pointer-events-none absolute left-3 top-3 max-w-[40ch] rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)]/90 px-3 py-2 text-xs text-[#f48771]">
          {errorMsg}
        </div>
      )}
    </div>
  );
}
