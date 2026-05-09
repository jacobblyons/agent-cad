import { useEffect, useRef, useState } from "react";
import { TweaksPanel } from "./TweaksPanel";
import { ObjectBrowser } from "./ObjectBrowser";
import { SketchBrowser } from "./SketchBrowser";
import { ImportBrowser } from "./ImportBrowser";

// Fraction of the right-sidebar height given to the browsers (objects /
// sketches / imports). Stored as a 0..1 ratio rather than pixels so the
// split scales with the window — the previous fixed 220px ate the screen
// at low heights and looked tiny on big monitors.
const STORAGE_KEY = "agent-cad:right-sidebar:bottom-fraction";
const DEFAULT_FRACTION = 0.5;
const MIN_FRACTION = 0.12;
const MAX_FRACTION = 0.88;

export function RightSidebar() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [bottomFraction, setBottomFraction] = useState<number>(() => {
    const raw = typeof window !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
    const n = raw ? parseFloat(raw) : NaN;
    if (!Number.isFinite(n)) return DEFAULT_FRACTION;
    return Math.min(MAX_FRACTION, Math.max(MIN_FRACTION, n));
  });
  const dragStateRef = useRef<{ startY: number; startFraction: number } | null>(null);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, bottomFraction.toFixed(4));
  }, [bottomFraction]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragStateRef.current = { startY: e.clientY, startFraction: bottomFraction };
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStateRef.current;
    if (!drag) return;
    const containerH = containerRef.current?.clientHeight ?? 1;
    if (containerH <= 0) return;
    // Dragging up grows the bottom (browsers) pane; convert pixel delta
    // into a fraction delta against the live container height.
    const deltaFraction = (drag.startY - e.clientY) / containerH;
    const next = Math.min(
      MAX_FRACTION,
      Math.max(MIN_FRACTION, drag.startFraction + deltaFraction),
    );
    setBottomFraction(next);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragStateRef.current) {
      (e.currentTarget as Element).releasePointerCapture(e.pointerId);
      dragStateRef.current = null;
    }
  };

  const topFlex = 1 - bottomFraction;
  const bottomFlex = bottomFraction;

  return (
    <div ref={containerRef} className="flex h-full min-h-0 flex-col">
      <div
        className="min-h-0"
        style={{ flex: `${topFlex} 1 0`, minHeight: 80 }}
      >
        <TweaksPanel />
      </div>
      <div
        role="separator"
        aria-orientation="horizontal"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        className="group relative h-1 shrink-0 cursor-row-resize bg-[var(--color-border)] hover:bg-[var(--color-focus)]"
        title="Drag to resize"
      >
        {/* invisible thicker hit area */}
        <div className="absolute inset-x-0 -top-1 -bottom-1" />
      </div>
      <div
        className="flex flex-col min-h-0"
        style={{ flex: `${bottomFlex} 1 0`, minHeight: 120 }}
      >
        <div className="min-h-0 flex-1 border-b border-[var(--color-border)]">
          <ObjectBrowser />
        </div>
        <div className="min-h-0 flex-1 border-b border-[var(--color-border)]">
          <SketchBrowser />
        </div>
        <div className="min-h-0 flex-1">
          <ImportBrowser />
        </div>
      </div>
    </div>
  );
}
