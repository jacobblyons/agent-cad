import { useEffect, useRef, useState } from "react";
import { TweaksPanel } from "./TweaksPanel";
import { ObjectBrowser } from "./ObjectBrowser";
import { SketchBrowser } from "./SketchBrowser";

const STORAGE_KEY = "agent-cad:right-sidebar:objects-h";
const DEFAULT_PX = 220;
const MIN_OBJECTS_PX = 80;
const MIN_TWEAKS_PX = 80;

export function RightSidebar() {
  const containerRef = useRef<HTMLDivElement>(null);
  const [objectsHeight, setObjectsHeight] = useState<number>(() => {
    const raw = typeof window !== "undefined" ? localStorage.getItem(STORAGE_KEY) : null;
    const n = raw ? parseInt(raw, 10) : NaN;
    return Number.isFinite(n) ? n : DEFAULT_PX;
  });
  const dragStateRef = useRef<{ startY: number; startH: number } | null>(null);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, String(Math.round(objectsHeight)));
  }, [objectsHeight]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragStateRef.current = { startY: e.clientY, startH: objectsHeight };
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragStateRef.current;
    if (!drag) return;
    const delta = drag.startY - e.clientY; // dragging up grows objects pane
    const containerH = containerRef.current?.clientHeight ?? 600;
    const max = Math.max(MIN_OBJECTS_PX, containerH - MIN_TWEAKS_PX);
    const next = Math.min(max, Math.max(MIN_OBJECTS_PX, drag.startH + delta));
    setObjectsHeight(next);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (dragStateRef.current) {
      (e.currentTarget as Element).releasePointerCapture(e.pointerId);
      dragStateRef.current = null;
    }
  };

  return (
    <div ref={containerRef} className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1">
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
        className="shrink-0 flex flex-col min-h-0"
        style={{ height: `${objectsHeight}px` }}
      >
        <div className="min-h-0 flex-1 border-b border-[var(--color-border)]">
          <ObjectBrowser />
        </div>
        <div className="min-h-0 flex-1">
          <SketchBrowser />
        </div>
      </div>
    </div>
  );
}
