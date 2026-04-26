import { useEffect, useMemo, useRef, useState } from "react";
import { Eraser, Pencil, Trash2, Undo2 } from "lucide-react";
import { Dialog, PrimaryButton, SecondaryButton } from "./Dialog";
import { cn } from "@/lib/utils";

const DEFAULT_W = 800;
const DEFAULT_H = 500;
const MAX_W = 1200;
const MAX_H = 800;
const BG_FILL = "#ffffff";
const PRESET_COLORS = ["#111111", "#e02424", "#1d4ed8", "#15803d", "#ca8a04", "#a855f7"];

type Tool = "pen" | "eraser";

type Props = {
  open: boolean;
  onClose: () => void;
  onAttach: (image: { data: string; mimeType: string }) => void;
  /** Optional dataURL background to annotate over. */
  background?: string | null;
};

/**
 * Layered drawing surface:
 *  - `bgRef` holds the optional image to annotate over (or a white fill).
 *  - `annRef` is an offscreen canvas with just the user's strokes (alpha).
 *  - `canvasRef` (visible) is composited from bg + annotations on every paint.
 *
 * Undo/clear only affect the annotations layer, so the background never gets
 * eaten by the eraser (which uses `destination-out`).
 */
export function DrawingDialog({ open, onClose, onAttach, background }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const annRef = useRef<HTMLCanvasElement | null>(null);
  const bgImgRef = useRef<HTMLImageElement | null>(null);

  const [tool, setTool] = useState<Tool>("pen");
  const [color, setColor] = useState("#111111");
  const [size, setSize] = useState(4);
  // ImageData snapshots of the annotations layer, one per completed stroke.
  const [history, setHistory] = useState<ImageData[]>([]);
  // Drives canvas resize when the bg image loads.
  const [dims, setDims] = useState<{ w: number; h: number }>({ w: DEFAULT_W, h: DEFAULT_H });

  const title = useMemo(
    () => (background ? "Annotate snapshot" : "Sketch attachment"),
    [background],
  );

  // Initialize / re-initialize when the dialog opens (or background changes).
  useEffect(() => {
    if (!open) return;
    setTool("pen");
    setHistory([]);

    const initWith = (w: number, h: number, bg: HTMLImageElement | null) => {
      bgImgRef.current = bg;
      setDims({ w, h });
      // Defer canvas wiring one tick so the new dims have applied to the DOM.
      requestAnimationFrame(() => {
        const visible = canvasRef.current;
        if (!visible) return;
        annRef.current = document.createElement("canvas");
        annRef.current.width = w;
        annRef.current.height = h;
        composite();
      });
    };

    if (background) {
      const img = new Image();
      img.onload = () => {
        const ar = img.naturalWidth / img.naturalHeight;
        let w = img.naturalWidth;
        let h = img.naturalHeight;
        if (w > MAX_W) {
          w = MAX_W;
          h = Math.round(MAX_W / ar);
        }
        if (h > MAX_H) {
          h = MAX_H;
          w = Math.round(MAX_H * ar);
        }
        initWith(w, h, img);
      };
      img.onerror = () => initWith(DEFAULT_W, DEFAULT_H, null);
      img.src = background;
    } else {
      initWith(DEFAULT_W, DEFAULT_H, null);
    }
  }, [open, background]);

  /** Repaint visible canvas: bg fill (or image), then annotations on top. */
  const composite = () => {
    const c = canvasRef.current;
    if (!c) return;
    const g = c.getContext("2d");
    if (!g) return;
    g.save();
    g.globalCompositeOperation = "source-over";
    g.fillStyle = BG_FILL;
    g.fillRect(0, 0, c.width, c.height);
    if (bgImgRef.current) {
      g.drawImage(bgImgRef.current, 0, 0, c.width, c.height);
    }
    if (annRef.current) {
      g.drawImage(annRef.current, 0, 0);
    }
    g.restore();
  };

  const annCtx = () => annRef.current?.getContext("2d") ?? null;

  const handleUndo = () => {
    setHistory((h) => {
      if (h.length === 0) return h;
      const next = h.slice(0, -1);
      const ann = annRef.current;
      const g = annCtx();
      if (ann && g) {
        g.clearRect(0, 0, ann.width, ann.height);
        const target = next[next.length - 1];
        if (target) g.putImageData(target, 0, 0);
      }
      composite();
      return next;
    });
  };

  const handleClear = () => {
    const ann = annRef.current;
    const g = annCtx();
    if (ann && g) g.clearRect(0, 0, ann.width, ann.height);
    setHistory([]);
    composite();
  };

  const drawing = useRef<{ active: boolean; lastX: number; lastY: number }>({
    active: false,
    lastX: 0,
    lastY: 0,
  });

  const localCoords = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const c = canvasRef.current!;
    const rect = c.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) / rect.width) * c.width,
      y: ((e.clientY - rect.top) / rect.height) * c.height,
    };
  };

  const applyBrush = (g: CanvasRenderingContext2D) => {
    g.lineCap = "round";
    g.lineJoin = "round";
    g.lineWidth = size;
    if (tool === "eraser") {
      g.globalCompositeOperation = "destination-out";
      g.strokeStyle = "#000";
      g.fillStyle = "#000";
    } else {
      g.globalCompositeOperation = "source-over";
      g.strokeStyle = color;
      g.fillStyle = color;
    }
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    e.preventDefault();
    canvasRef.current?.setPointerCapture(e.pointerId);
    const { x, y } = localCoords(e);
    drawing.current = { active: true, lastX: x, lastY: y };
    const g = annCtx();
    if (!g) return;
    applyBrush(g);
    g.beginPath();
    g.arc(x, y, size / 2, 0, Math.PI * 2);
    g.fill();
    composite();
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawing.current.active) return;
    const g = annCtx();
    if (!g) return;
    const { x, y } = localCoords(e);
    applyBrush(g);
    g.beginPath();
    g.moveTo(drawing.current.lastX, drawing.current.lastY);
    g.lineTo(x, y);
    g.stroke();
    drawing.current.lastX = x;
    drawing.current.lastY = y;
    composite();
  };

  const endStroke = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawing.current.active) return;
    drawing.current.active = false;
    canvasRef.current?.releasePointerCapture(e.pointerId);
    const ann = annRef.current;
    const g = annCtx();
    if (!ann || !g) return;
    const snap = g.getImageData(0, 0, ann.width, ann.height);
    setHistory((h) => [...h, snap]);
  };

  const handleAttach = () => {
    const c = canvasRef.current;
    if (!c) return;
    const dataUrl = c.toDataURL("image/png");
    const comma = dataUrl.indexOf(",");
    const data = comma >= 0 ? dataUrl.slice(comma + 1) : dataUrl;
    onAttach({ data, mimeType: "image/png" });
    onClose();
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={title}
      width="w-[860px]"
      footer={
        <>
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
          <PrimaryButton onClick={handleAttach}>Attach</PrimaryButton>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3 text-xs text-[var(--color-text)]">
          <ToolButton active={tool === "pen"} onClick={() => setTool("pen")} label="Pen">
            <Pencil size={14} />
          </ToolButton>
          <ToolButton active={tool === "eraser"} onClick={() => setTool("eraser")} label="Eraser">
            <Eraser size={14} />
          </ToolButton>

          <div className="flex items-center gap-1">
            {PRESET_COLORS.map((c) => (
              <button
                key={c}
                onClick={() => {
                  setColor(c);
                  setTool("pen");
                }}
                aria-label={`color ${c}`}
                className={cn(
                  "h-5 w-5 rounded-sm border",
                  color === c && tool === "pen"
                    ? "border-[var(--color-focus)] ring-1 ring-[var(--color-focus)]"
                    : "border-[var(--color-border)]",
                )}
                style={{ background: c }}
              />
            ))}
            <input
              type="color"
              value={color}
              onChange={(e) => {
                setColor(e.target.value);
                setTool("pen");
              }}
              className="ml-1 h-5 w-6 cursor-pointer rounded-sm border border-[var(--color-border)] bg-transparent"
              aria-label="custom color"
            />
          </div>

          <label className="flex items-center gap-2">
            <span className="text-[var(--color-muted)]">Size</span>
            <input
              type="range"
              min={1}
              max={32}
              value={size}
              onChange={(e) => setSize(parseInt(e.target.value, 10))}
              className="w-24"
            />
            <span className="w-6 text-right tabular-nums text-[var(--color-muted)]">{size}</span>
          </label>

          <div className="ml-auto flex items-center gap-1">
            <ToolButton onClick={handleUndo} disabled={history.length === 0} label="Undo">
              <Undo2 size={14} />
            </ToolButton>
            <ToolButton onClick={handleClear} label="Clear">
              <Trash2 size={14} />
            </ToolButton>
          </div>
        </div>

        <div className="flex justify-center rounded-sm border border-[var(--color-border)] bg-[var(--color-panel-2)] p-2">
          <canvas
            ref={canvasRef}
            width={dims.w}
            height={dims.h}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endStroke}
            onPointerCancel={endStroke}
            onPointerLeave={endStroke}
            style={{ touchAction: "none", maxHeight: "60vh" }}
            className={cn(
              "max-w-full rounded-sm bg-white shadow-sm",
              tool === "eraser" ? "cursor-cell" : "cursor-crosshair",
            )}
          />
        </div>
      </div>
    </Dialog>
  );
}

function ToolButton({
  active,
  disabled,
  onClick,
  label,
  children,
}: {
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className={cn(
        "flex h-7 items-center gap-1.5 rounded-sm border px-2 text-xs",
        active
          ? "border-[var(--color-focus)] bg-[var(--color-selection)] text-[var(--color-text)]"
          : "border-[var(--color-border)] text-[var(--color-text)] hover:bg-[var(--color-hover)]",
        disabled && "opacity-40",
      )}
    >
      {children}
      <span>{label}</span>
    </button>
  );
}
