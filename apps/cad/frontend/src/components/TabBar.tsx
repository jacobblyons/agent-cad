import { useState } from "react";
import {
  Download,
  FolderOpen,
  Loader2,
  Plus,
  Printer,
  RefreshCw,
  Save,
  Settings,
  X,
} from "lucide-react";
import { call } from "@/lib/pywebview";
import { useDoc } from "@/lib/doc";
import { useChat } from "@/lib/chat";
import { usePrint } from "@/lib/print";
import { useTabs } from "@/lib/tabs";
import { useUi } from "@/lib/ui";
import { cn } from "@/lib/utils";
import { BrowserBadge } from "./BrowserPanel";

export function TabBar() {
  const { doc } = useDoc();
  const { isAgentRunning } = useChat();
  const { tabs, activeId, focusTab, closeTab } = useTabs();
  const ui = useUi();
  const print = usePrint();
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exporting, setExporting] = useState(false);

  const enterPrint = async () => {
    if (!doc || print.busy || print.active) return;
    if (print.printers.length === 0) {
      const goSettings = window.confirm(
        "No printers configured yet. Open Settings to add one?",
      );
      if (goSettings) ui.openSettings();
      return;
    }
    await print.enter();
  };

  const exportCombined = async () => {
    if (!doc || exporting) return;
    setExporting(true);
    try {
      const r = await call<{ ok: boolean; error?: string; cancelled?: boolean }>(
        "project_export_combined",
        doc.id,
      );
      if (!r?.ok && !r?.cancelled) {
        window.alert(`Export failed: ${r?.error ?? "unknown error"}`);
      }
    } finally {
      setExporting(false);
    }
  };

  const refresh = async () => {
    if (!doc || refreshing) return;
    setRefreshing(true);
    try {
      await call("project_refresh", doc.id);
    } finally {
      setTimeout(() => setRefreshing(false), 250);
    }
  };

  const save = async () => {
    if (!doc || saving) return;
    setSaving(true);
    try {
      await call("project_commit", doc.id, "save");
    } finally {
      setTimeout(() => setSaving(false), 300);
    }
  };

  return (
    <div className="flex h-10 shrink-0 items-stretch border-b border-[var(--color-border)] bg-[var(--color-panel-2)] px-1">
      <div className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto">
        {tabs.map((t) => {
          const active = t.doc.id === activeId;
          const running =
            active && isAgentRunning
              ? true
              : t.turns.some(
                  (turn) => turn.role === "assistant" && turn.status === "running",
                );
          return (
            <div
              key={t.doc.id}
              role="tab"
              aria-selected={active}
              onMouseDown={(e) => {
                if (e.button === 1) {
                  e.preventDefault();
                  closeTab(t.doc.id);
                  return;
                }
                if (e.button === 0) focusTab(t.doc.id);
              }}
              className={cn(
                "group relative flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-sm border px-2.5 text-sm",
                active
                  ? "border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]"
                  : "border-transparent text-[var(--color-muted)] hover:bg-[var(--color-panel)] hover:text-[var(--color-text)]",
              )}
            >
              <span className="max-w-[180px] truncate">{t.doc.title}</span>
              {running ? (
                <Loader2 size={10} className="shrink-0 animate-spin opacity-70" />
              ) : t.doc.uncommitted ? (
                <span
                  title="uncommitted changes"
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#dcb073]"
                />
              ) : null}
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  closeTab(t.doc.id);
                }}
                aria-label="close tab"
                className={cn(
                  "ml-0.5 rounded p-0.5 transition",
                  active
                    ? "opacity-50 hover:bg-[var(--color-hover)] hover:opacity-100"
                    : "opacity-0 group-hover:opacity-50 hover:bg-[var(--color-hover)] hover:!opacity-100",
                )}
              >
                <X size={11} />
              </button>
            </div>
          );
        })}
        <ToolbarBtn
          onClick={ui.openNew}
          title="New project (Ctrl+N)"
          className="ml-1"
        >
          <Plus size={14} />
        </ToolbarBtn>
        <ToolbarBtn onClick={ui.openOpen} title="Open project (Ctrl+O)">
          <FolderOpen size={14} />
        </ToolbarBtn>
      </div>

      <div className="flex shrink-0 items-center gap-0.5 pl-1">
        <BrowserBadge />
        {doc && (
          <span
            className="mr-2 max-w-[260px] truncate font-mono text-[10px] text-[var(--color-muted)]"
            title={doc.path}
          >
            {doc.head_branch} · {doc.head_sha.slice(0, 7)}
          </span>
        )}
        <ToolbarBtn
          onClick={exportCombined}
          disabled={!doc || exporting}
          title="Export every visible object as STL / STEP / BREP"
        >
          <Download size={13} className={cn(exporting && "animate-pulse")} />
        </ToolbarBtn>
        <ToolbarBtn
          onClick={enterPrint}
          disabled={!doc || print.busy || print.active}
          title={
            print.active
              ? "Already in the print phase"
              : print.printers.length === 0
                ? "Configure a 3D printer in Settings to enable"
                : "Enter the print phase — slice + send to a 3D printer"
          }
        >
          <Printer size={13} className={cn(print.busy && "animate-pulse")} />
        </ToolbarBtn>
        <ToolbarBtn
          onClick={save}
          disabled={!doc || saving}
          title="Save (Ctrl+S) — commit current state to the timeline"
        >
          <Save size={13} />
        </ToolbarBtn>
        <ToolbarBtn
          onClick={refresh}
          disabled={!doc || refreshing}
          title="Refresh (Ctrl+R) — re-run model.py"
        >
          <RefreshCw size={13} className={cn(refreshing && "animate-spin")} />
        </ToolbarBtn>
        <ToolbarBtn onClick={ui.openSettings} title="Settings (Ctrl+,)">
          <Settings size={14} />
        </ToolbarBtn>
      </div>
    </div>
  );
}

function ToolbarBtn(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className, ...rest } = props;
  return (
    <button
      {...rest}
      className={cn(
        "flex h-8 w-8 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-panel)] hover:text-[var(--color-text)] disabled:opacity-40",
        className,
      )}
    />
  );
}
