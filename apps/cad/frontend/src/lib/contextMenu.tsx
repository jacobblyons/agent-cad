/**
 * Tiny context-menu primitive: a provider + hook + floating panel.
 *
 * Usage:
 *   1. Wrap the app once in <ContextMenuHost>...</ContextMenuHost>.
 *   2. Inside any component, useContextMenu() to get the opener.
 *   3. Call openContextMenu(e, items) from an onContextMenu handler.
 *
 * One menu is on screen at a time. Click outside, press Escape, or pick
 * an item to dismiss. Position is clamped to the viewport so the menu
 * never spills off the edge.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

import { cn } from "./utils";

// Loose enough to accept lucide-react icons (which take `size` as
// number | string) without forcing callers to wrap them.
type IconType = React.ComponentType<{
  size?: number | string;
  className?: string;
}>;

export type MenuItem =
  | {
      kind: "action";
      label: string;
      icon?: IconType;
      onClick: () => void;
      disabled?: boolean;
      danger?: boolean;
      hint?: string;
    }
  | { kind: "separator" };

type Request = { x: number; y: number; items: MenuItem[] };

type Ctx = {
  open: (req: Request) => void;
  close: () => void;
};

const ContextMenuCtx = createContext<Ctx | null>(null);

export function ContextMenuHost({ children }: { children: React.ReactNode }) {
  const [req, setReq] = useState<Request | null>(null);

  const close = useCallback(() => setReq(null), []);
  const open = useCallback((r: Request) => setReq(r), []);

  return (
    <ContextMenuCtx.Provider value={{ open, close }}>
      {children}
      {req && <Panel req={req} onClose={close} />}
    </ContextMenuCtx.Provider>
  );
}

/** Returns a function to open the menu from an onContextMenu handler.
 * The function takes the event (so it can preventDefault + grab coords)
 * and the list of items to show. Empty/all-disabled lists are a no-op. */
export function useContextMenu() {
  const ctx = useContext(ContextMenuCtx);
  if (!ctx) throw new Error("useContextMenu must be used inside <ContextMenuHost>");
  return useCallback(
    (e: React.MouseEvent, items: MenuItem[]) => {
      const visible = items.filter(
        (it) => it.kind !== "action" || !it.disabled,
      );
      if (visible.length === 0) return;
      e.preventDefault();
      e.stopPropagation();
      ctx.open({ x: e.clientX, y: e.clientY, items });
    },
    [ctx],
  );
}

function Panel({ req, onClose }: { req: Request; onClose: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ left: number; top: number }>({
    left: req.x,
    top: req.y,
  });

  // Clamp to viewport once the menu has measured itself. Off-screen
  // overflow is a real issue on small windows / corners.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const margin = 4;
    const maxLeft = window.innerWidth - r.width - margin;
    const maxTop = window.innerHeight - r.height - margin;
    setCoords({
      left: Math.min(Math.max(margin, req.x), Math.max(margin, maxLeft)),
      top: Math.min(Math.max(margin, req.y), Math.max(margin, maxTop)),
    });
  }, [req.x, req.y]);

  // Esc / outside click closes. Outside click is captured at document so
  // we still see clicks on top-layer elements.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onPointer = (e: MouseEvent) => {
      if (!ref.current) return;
      if (!ref.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer, true);
    document.addEventListener("contextmenu", onPointer, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer, true);
      document.removeEventListener("contextmenu", onPointer, true);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      style={{ left: coords.left, top: coords.top }}
      className="fixed z-[200] min-w-[180px] rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] py-1 text-xs shadow-xl"
      onContextMenu={(e) => e.preventDefault()}
    >
      {req.items.map((it, i) => {
        if (it.kind === "separator") {
          return <div key={i} className="my-1 h-px bg-[var(--color-border)]" />;
        }
        const Icon = it.icon;
        return (
          <button
            key={i}
            disabled={it.disabled}
            onClick={() => {
              if (it.disabled) return;
              onClose();
              it.onClick();
            }}
            className={cn(
              "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[var(--color-text)]",
              "hover:bg-[var(--color-selection)]",
              it.disabled && "cursor-not-allowed opacity-40 hover:bg-transparent",
              it.danger && !it.disabled && "text-[#f48771] hover:bg-[#3a1d1d]",
            )}
          >
            {Icon ? (
              <Icon size={11} className="shrink-0" />
            ) : (
              <span className="inline-block w-[11px] shrink-0" />
            )}
            <span className="flex-1 truncate">{it.label}</span>
            {it.hint && (
              <span className="shrink-0 font-mono text-[10px] text-[var(--color-muted)]">
                {it.hint}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
