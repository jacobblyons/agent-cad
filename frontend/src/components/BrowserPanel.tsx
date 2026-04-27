import { useEffect, useRef, useState } from "react";
import { ChevronUp, GripHorizontal, Globe, Loader2, MousePointer, X } from "lucide-react";
import { useBrowser } from "@/lib/browser";
import { call } from "@/lib/pywebview";
import { cn } from "@/lib/utils";

const STALE_FRAME_MS = 5_000;
const STORAGE_KEY = "agent-cad:browser-panel:rect";
const DEFAULT_WIDTH = 720;
const DEFAULT_HEIGHT = 480;
const MIN_WIDTH = 360;
const MIN_HEIGHT = 220;
const HEADER_HEIGHT = 32;

type Rect = { x: number; y: number; w: number; h: number };

function defaultRect(): Rect {
  // Bottom-right corner, with a margin.
  const w = DEFAULT_WIDTH;
  const h = DEFAULT_HEIGHT;
  if (typeof window === "undefined") return { x: 64, y: 64, w, h };
  return {
    x: Math.max(16, window.innerWidth - w - 24),
    y: Math.max(64, window.innerHeight - h - 80),
    w,
    h,
  };
}

function loadRect(): Rect {
  if (typeof window === "undefined") return defaultRect();
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultRect();
    const r = JSON.parse(raw);
    if (
      typeof r?.x === "number" &&
      typeof r?.y === "number" &&
      typeof r?.w === "number" &&
      typeof r?.h === "number"
    ) {
      return clampRect(r);
    }
  } catch {
    /* ignore */
  }
  return defaultRect();
}

function clampRect(r: Rect): Rect {
  const w = Math.max(MIN_WIDTH, r.w);
  const h = Math.max(MIN_HEIGHT, r.h);
  const maxX = Math.max(0, window.innerWidth - w);
  const maxY = Math.max(0, window.innerHeight - h);
  return {
    x: Math.min(maxX, Math.max(0, r.x)),
    y: Math.min(maxY, Math.max(0, r.y)),
    w,
    h,
  };
}

/**
 * Floating, draggable, resizable window showing the agent's live
 * Chromium screencast. Hidden until the agent first opens a page,
 * then auto-shows in the bottom-right with a remembered position.
 *
 * Frames stream in via the bus as base64 JPEGs; we just plug the
 * latest one into an `<img>`. View-only — the agent drives input
 * through Playwright MCP, the user just watches.
 */
export function BrowserPanel() {
  const browser = useBrowser();
  const [stale, setStale] = useState(false);
  const [rect, setRect] = useState<Rect>(() => loadRect());
  const [interactive, setInteractive] = useState(false);
  const dragRef = useRef<{ startX: number; startY: number; rx: number; ry: number } | null>(null);
  const stageRef = useRef<HTMLDivElement>(null);

  // Tick a "stale" flag if no new frames have come in for a while.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (browser.lastFrameAt == null) {
        setStale(false);
        return;
      }
      setStale(Date.now() - browser.lastFrameAt > STALE_FRAME_MS);
    }, 1000);
    return () => window.clearInterval(id);
  }, [browser.lastFrameAt]);

  // Persist position + size whenever they change.
  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(rect));
    } catch {
      /* ignore quota errors */
    }
  }, [rect]);

  // Re-clamp on viewport resize so dragging out of bounds in a previous
  // session doesn't leave the window unreachable.
  useEffect(() => {
    const onResize = () => setRect((r) => clampRect(r));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  // Track CSS-resize via a ResizeObserver so dragging the corner updates
  // our persisted rect (otherwise the size resets when the component
  // re-renders for a new frame).
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      const w = Math.round(e.contentRect.width);
      const h = Math.round(e.contentRect.height);
      setRect((r) => (r.w === w && r.h === h ? r : { ...r, w, h }));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const onDragPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragRef.current = {
      startX: e.clientX,
      startY: e.clientY,
      rx: rect.x,
      ry: rect.y,
    };
  };

  const onDragPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    setRect((r) => clampRect({ ...r, x: d.rx + dx, y: d.ry + dy }));
  };

  const onDragPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragRef.current) {
      (e.currentTarget as Element).releasePointerCapture(e.pointerId);
      dragRef.current = null;
    }
  };

  // ---- input forwarding -------------------------------------------
  // When the user has flipped on Interact mode, pointer + keyboard
  // events on the screencast img get translated to CDP coordinates and
  // shipped back to Python. Useful for "I'll solve the CAPTCHA, you
  // keep going" handoffs.

  const deviceCoords = (e: { clientX: number; clientY: number },
                        target: HTMLElement) => {
    const r = target.getBoundingClientRect();
    const dw = browser.frame?.deviceWidth ?? r.width;
    const dh = browser.frame?.deviceHeight ?? r.height;
    return {
      x: ((e.clientX - r.left) / Math.max(r.width, 1)) * dw,
      y: ((e.clientY - r.top) / Math.max(r.height, 1)) * dh,
    };
  };

  const stageHandlers = interactive
    ? {
        onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
          if (!browser.frame) return;
          e.preventDefault();
          stageRef.current?.focus();
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
          const { x, y } = deviceCoords(e, e.currentTarget);
          call("browser_send_input", "mouse_press", {
            x, y, button: "left", click_count: 1, buttons: 1,
          });
        },
        onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
          if (!browser.frame) return;
          // Skip move while no button is held — too chatty otherwise.
          if (e.buttons === 0) return;
          const { x, y } = deviceCoords(e, e.currentTarget);
          call("browser_send_input", "mouse_move", {
            x, y, buttons: e.buttons,
          });
        },
        onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => {
          if (!browser.frame) return;
          (e.currentTarget as HTMLElement).releasePointerCapture(e.pointerId);
          const { x, y } = deviceCoords(e, e.currentTarget);
          call("browser_send_input", "mouse_release", {
            x, y, button: "left", buttons: 0,
          });
        },
        onWheel: (e: React.WheelEvent<HTMLDivElement>) => {
          if (!browser.frame) return;
          e.preventDefault();
          const { x, y } = deviceCoords(e, e.currentTarget);
          call("browser_send_input", "wheel", {
            x, y, delta_x: e.deltaX, delta_y: e.deltaY,
          });
        },
        onKeyDown: (e: React.KeyboardEvent<HTMLDivElement>) => {
          // Single printable characters — just insert text. Sites
          // that read input events get a clean signal this way.
          if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
            e.preventDefault();
            call("browser_send_input", "insert_text", { text: e.key });
            return;
          }
          // Special keys (Enter, Backspace, etc.) → CDP key events.
          const isNav = [
            "Enter", "Tab", "Backspace", "Escape",
            "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
            "Home", "End", "PageUp", "PageDown", "Delete",
          ].includes(e.key);
          if (isNav) {
            e.preventDefault();
            call("browser_send_input", "key_down", {
              key: e.key, code: e.code,
            });
            call("browser_send_input", "key_up", {
              key: e.key, code: e.code,
            });
          }
        },
      }
    : {};

  // The panel only renders while it's expanded. The agent can be busy
  // and the window collapsed; the BrowserBadge in the TabBar surfaces
  // that state and re-expands on click.
  if (browser.collapsed) return null;

  return (
    <div
      ref={containerRef}
      role="dialog"
      aria-label="Embedded browser preview"
      style={{
        position: "fixed",
        left: rect.x,
        top: rect.y,
        width: rect.w,
        height: rect.h,
        resize: "both",
        overflow: "hidden",
        zIndex: 40,
      }}
      className="flex flex-col rounded-md border border-[var(--color-border-strong)] bg-[var(--color-panel)] shadow-2xl"
    >
      <div
        onPointerDown={onDragPointerDown}
        onPointerMove={onDragPointerMove}
        onPointerUp={onDragPointerUp}
        onPointerCancel={onDragPointerUp}
        className="flex shrink-0 cursor-move items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-panel-2)] px-2"
        style={{ height: HEADER_HEIGHT }}
      >
        <GripHorizontal size={11} className="shrink-0 text-[var(--color-muted)]" />
        <Globe size={12} className="shrink-0 text-[var(--color-muted)]" />
        <span className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
          Browser
        </span>
        <span
          className="min-w-0 flex-1 truncate font-mono text-[11px] text-[var(--color-text)]"
          title={browser.url ?? ""}
        >
          {browser.url ?? "(no page yet)"}
        </span>
        {browser.active && !stale && (
          <span className="flex items-center gap-1 text-[10px] text-[var(--color-accent)]">
            <Loader2 size={10} className="animate-spin" />
            <span>live</span>
          </span>
        )}
        {stale && (
          <span className="text-[10px] text-[var(--color-muted)]">idle</span>
        )}
        <button
          onClick={(e) => {
            e.stopPropagation();
            setInteractive((v) => !v);
            if (!interactive) {
              // Schedule focus AFTER the toggle so React paints and
              // the stage element is interactive before we focus it.
              setTimeout(() => stageRef.current?.focus(), 0);
            }
          }}
          title={
            interactive
              ? "Disable click + keyboard passthrough"
              : "Enable click + keyboard passthrough (e.g. for CAPTCHAs)"
          }
          className={cn(
            "flex h-6 items-center gap-1 rounded-sm px-1.5 text-[10px] uppercase tracking-wider",
            interactive
              ? "bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)]"
              : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
          )}
        >
          <MousePointer size={11} />
          <span>{interactive ? "interact" : "view"}</span>
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            browser.setCollapsed(true);
          }}
          title="Hide window"
          className="flex h-6 w-6 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
        >
          <X size={12} />
        </button>
      </div>

      <div
        ref={stageRef}
        tabIndex={interactive ? 0 : -1}
        {...stageHandlers}
        style={{ touchAction: interactive ? "none" : "auto" }}
        className={cn(
          "flex min-h-0 flex-1 items-center justify-center overflow-auto bg-[#1e1e1e] p-1.5 outline-none",
          interactive
            ? "cursor-crosshair focus:ring-1 focus:ring-[var(--color-focus)]"
            : "cursor-default",
          stale && "opacity-70",
        )}
      >
        {browser.frame ? (
          <img
            src={`data:${browser.frame.mime};base64,${browser.frame.data}`}
            alt="agent browser screencast"
            className="block max-h-full max-w-full rounded-sm border border-[var(--color-border)] object-contain"
            draggable={false}
          />
        ) : (
          <div className="flex flex-col items-center gap-2 text-xs text-[var(--color-muted)]">
            <Loader2 size={14} className="animate-spin opacity-50" />
            <span>waiting for the first frame…</span>
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Tiny indicator shown in the TabBar when the panel is collapsed but a
 * browser session is active. Clicking re-expands the panel.
 */
export function BrowserBadge() {
  const browser = useBrowser();
  if (!browser.active || !browser.collapsed) return null;
  return (
    <button
      onClick={() => browser.setCollapsed(false)}
      title={browser.url ?? "Show browser window"}
      className="ml-1 flex h-7 items-center gap-1 rounded-sm border border-[var(--color-border)] bg-[var(--color-panel-2)] px-2 text-[10px] uppercase tracking-wider text-[var(--color-accent)] hover:bg-[var(--color-hover)]"
    >
      <Globe size={11} />
      <span>browser</span>
      <ChevronUp size={11} />
    </button>
  );
}
