/**
 * Agent Slicer — skeleton shell.
 *
 * v0.2.0 Phase 3: this lays out the chrome (plate tabs, viewport,
 * properties + chat panel) but contains no real logic. Subsequent
 * phases fill in:
 *   - Phase 4: 3MF model + plate manager (backend), live data here
 *   - Phase 5: Three.js bed + model rendering in the viewport
 *   - Phase 6: drag/rotate handles, move-to-plate context menu
 *   - Phase 7: chat panel + agent runner (mcp__slicer__* tools)
 */
import { useEffect, useState } from "react";
import { Plus, Send } from "lucide-react";

declare global {
  interface Window {
    pywebview?: {
      api: {
        ping: () => Promise<string>;
        get_settings: () => Promise<Record<string, unknown>>;
      };
    };
  }
}

function PlateTabs({
  plates,
  activeIndex,
  onSelect,
  onAdd,
}: {
  plates: { index: number; name: string }[];
  activeIndex: number;
  onSelect: (i: number) => void;
  onAdd: () => void;
}) {
  return (
    <div className="flex items-center gap-1 border-b border-[var(--color-border)] bg-[var(--color-panel)] px-3 py-1.5">
      {plates.map((p) => (
        <button
          key={p.index}
          onClick={() => onSelect(p.index)}
          className={
            "rounded-sm px-3 py-1 text-xs " +
            (p.index === activeIndex
              ? "bg-[var(--color-selection)] text-[var(--color-text)]"
              : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]")
          }
        >
          {p.name}
        </button>
      ))}
      <button
        onClick={onAdd}
        aria-label="Add plate"
        className="ml-1 flex h-6 w-6 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
      >
        <Plus size={14} />
      </button>
    </div>
  );
}

function PlateViewport() {
  // Phase 5 replaces this with a real react-three-fiber scene
  // (bed + models + selection handles).
  return (
    <div className="relative flex-1 bg-[var(--color-bg)]">
      <div
        aria-hidden
        className="absolute inset-8 rounded-md border border-dashed border-[var(--color-border-strong)]"
      />
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <div className="rounded-md bg-[var(--color-panel-2)]/80 px-4 py-2 text-sm text-[var(--color-muted)]">
          Plate viewport — placeholder. Phase 5 wires in the bed + 3MF models.
        </div>
      </div>
    </div>
  );
}

function PropertiesPanel() {
  return (
    <div className="border-b border-[var(--color-border)] bg-[var(--color-panel)] p-3 text-xs">
      <div className="mb-2 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
        Selection
      </div>
      <div className="text-[var(--color-muted)]">
        No model selected. Click a model on the plate to inspect / move / rotate it.
      </div>
    </div>
  );
}

function ChatPanelPlaceholder() {
  // Phase 7 replaces this with the real shared chat panel.
  return (
    <div className="flex flex-1 flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        <div className="rounded-md bg-[var(--color-panel-2)] px-3 py-2 text-sm text-[var(--color-muted)]">
          The slicer agent will live here in Phase 7. It'll be able to
          arrange models on plates, slice, and send to the printer for
          you. The same drawing / snapshot / paste-image flow as
          agent-cad.
        </div>
      </div>
      <div className="border-t border-[var(--color-border)] p-3">
        <div className="flex items-end gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)] px-3 py-2 opacity-60">
          <textarea
            disabled
            placeholder="Chat is wired up in Phase 7…"
            rows={1}
            className="min-h-[24px] flex-1 resize-none bg-transparent text-sm leading-relaxed outline-none placeholder:text-[var(--color-muted)]"
          />
          <button
            disabled
            aria-label="send"
            className="flex h-8 w-8 items-center justify-center rounded-sm bg-[var(--color-accent)] text-[var(--color-accent-fg)]"
          >
            <Send size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  // Stub plate list. Replaced by api.list_plates() when Phase 4 lands.
  const [plates] = useState([{ index: 1, name: "Plate 1" }]);
  const [activeIndex, setActiveIndex] = useState(1);
  const [pingResult, setPingResult] = useState<string>("(unknown)");

  useEffect(() => {
    // Cheap end-to-end smoke: confirm the JS API is reachable.
    let cancelled = false;
    (async () => {
      try {
        // pywebview injects window.pywebview on first frame; poll briefly
        // for it before giving up.
        for (let i = 0; i < 20; i++) {
          if (window.pywebview?.api?.ping) break;
          await new Promise((r) => setTimeout(r, 50));
        }
        const r = await window.pywebview?.api.ping();
        if (!cancelled) setPingResult(r ?? "(no api)");
      } catch (e) {
        if (!cancelled) setPingResult(`error: ${(e as Error).message}`);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-panel)] px-4 py-2 text-sm">
        <div className="flex items-baseline gap-3">
          <span className="font-semibold">Agent Slicer</span>
          <span className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
            v0.0.1 · skeleton
          </span>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
          api: {pingResult}
        </div>
      </header>
      <div className="flex min-h-0 flex-1">
        {/* Center: plate tabs + viewport */}
        <div className="flex min-w-0 flex-1 flex-col">
          <PlateTabs
            plates={plates}
            activeIndex={activeIndex}
            onSelect={setActiveIndex}
            onAdd={() => {
              /* Phase 4 hooks api.add_plate */
            }}
          />
          <PlateViewport />
        </div>
        {/* Right: properties + chat */}
        <aside className="flex w-[360px] min-w-[280px] flex-col border-l border-[var(--color-border)]">
          <PropertiesPanel />
          <ChatPanelPlaceholder />
        </aside>
      </div>
    </div>
  );
}
