import { useState } from "react";
import {
  Check,
  Eye,
  EyeOff,
  FileBox,
  Loader2,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { call } from "@/lib/pywebview";
import { useDoc } from "@/lib/doc";
import { useChat } from "@/lib/chat";
import { useTabs } from "@/lib/tabs";
import { cn } from "@/lib/utils";
import { useContextMenu, type MenuItem } from "@/lib/contextMenu";

/**
 * Read-only reference geometry the user supplied. Same panel shape as
 * objects/sketches, but the rows can't be activated (imports are never
 * the agent's edit target — they're inputs).
 *
 * The "+" button opens a file picker via pick_file -> import_create on
 * the backend, copies the file in, and tessellates it.
 */
export function ImportBrowser() {
  const { doc } = useDoc();
  const { isAgentRunning } = useChat();
  const { tabs, activeId } = useTabs();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  if (!doc) return null;
  const imports = doc.imports ?? [];
  const activeTab = tabs.find((t) => t.doc.id === activeId);
  const isLoading = (name: string) =>
    activeTab?.importGeometry[name]?.loading === true;

  const addImport = async () => {
    if (isAgentRunning || adding) return;
    setAdding(true);
    try {
      await call<{ ok: boolean; cancelled?: boolean; error?: string }>(
        "import_pick_and_create",
        doc.id,
      );
    } finally {
      setAdding(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
        <span className="text-xs uppercase tracking-wider text-[var(--color-muted)]">
          Imports
        </span>
        <button
          onClick={addImport}
          disabled={isAgentRunning || adding}
          title="Import a STEP file"
          className="flex h-5 w-5 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)] disabled:opacity-40"
        >
          <Plus size={12} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {imports.length === 0 && (
          <div className="px-2 py-2 text-[11px] leading-relaxed text-[var(--color-muted)]">
            No imports yet. Click + to add a STEP reference model the agent can
            measure off and boolean against.
          </div>
        )}
        <div className="space-y-0.5">
          {imports.map((m) =>
            renaming === m.name ? (
              <RenameRow
                key={m.name}
                docId={doc.id}
                oldName={m.name}
                onDone={() => setRenaming(null)}
              />
            ) : (
              <ImportRow
                key={m.name}
                docId={doc.id}
                name={m.name}
                ext={m.ext}
                sizeBytes={m.size_bytes}
                visible={m.visible}
                loading={isLoading(m.name)}
                disabled={isAgentRunning}
                onRename={() => setRenaming(m.name)}
              />
            ),
          )}
        </div>
      </div>
    </div>
  );
}

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function ImportRow({
  docId,
  name,
  ext,
  sizeBytes,
  visible,
  loading,
  disabled,
  onRename,
}: {
  docId: string;
  name: string;
  ext: string;
  sizeBytes: number;
  visible: boolean;
  loading: boolean;
  disabled: boolean;
  onRename: () => void;
}) {
  const openMenu = useContextMenu();
  const toggleVisible = async () => {
    if (disabled) return;
    await call("import_set_visible", docId, name, !visible);
  };
  const del = async () => {
    if (disabled) return;
    if (
      !window.confirm(
        `Delete import "${name}"? Object scripts that reference it will break until updated.`,
      )
    )
      return;
    await call("import_delete", docId, name);
  };
  const onContextMenu = (e: React.MouseEvent) => {
    const items: MenuItem[] = [
      {
        kind: "action",
        label: visible ? "Hide" : "Show",
        icon: visible ? EyeOff : Eye,
        onClick: () => void toggleVisible(),
        disabled,
      },
      { kind: "separator" },
      {
        kind: "action",
        label: "Rename",
        icon: Pencil,
        onClick: onRename,
        disabled,
      },
      { kind: "separator" },
      {
        kind: "action",
        label: "Delete",
        icon: Trash2,
        onClick: () => void del(),
        disabled,
        danger: true,
      },
    ];
    openMenu(e, items);
  };
  const VisIcon = visible ? Eye : EyeOff;
  return (
    <div
      onContextMenu={onContextMenu}
      className={cn(
        "group flex items-center gap-1.5 rounded-sm px-2 py-1 text-xs",
        "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
        disabled && "opacity-50",
      )}
    >
      <button
        onClick={toggleVisible}
        disabled={disabled || loading}
        title={
          loading
            ? "Loading…"
            : visible
              ? "Hide in viewer"
              : "Show in viewer"
        }
        className={cn(
          "rounded p-0.5 transition hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
          loading
            ? "opacity-100 text-[var(--color-accent)]"
            : visible
              ? "opacity-0 group-hover:opacity-60 hover:!opacity-100"
              : "opacity-100 text-[var(--color-muted)]",
          (disabled && !loading) && "opacity-30",
        )}
      >
        {loading ? (
          <Loader2 size={11} className="animate-spin" />
        ) : (
          <VisIcon size={11} />
        )}
      </button>
      <FileBox size={11} className={cn("shrink-0", !visible && "opacity-40")} />
      <span
        className={cn("flex-1 truncate font-mono", !visible && "opacity-50")}
        title={`${name}.${ext} (${fmtBytes(sizeBytes)})`}
      >
        {name}
      </span>
      <span
        className={cn(
          "shrink-0 text-[10px] tabular-nums opacity-60",
          !visible && "opacity-30",
        )}
      >
        {fmtBytes(sizeBytes)}
      </span>
      <button
        onClick={onRename}
        disabled={disabled}
        title="Rename"
        className="rounded p-0.5 opacity-0 transition group-hover:opacity-60 hover:bg-[var(--color-hover)] hover:!opacity-100 disabled:opacity-0"
      >
        <Pencil size={10} />
      </button>
      <button
        onClick={del}
        disabled={disabled}
        title="Delete"
        className="rounded p-0.5 opacity-0 transition group-hover:opacity-60 hover:bg-[var(--color-hover)] hover:!opacity-100 disabled:opacity-0"
      >
        <Trash2 size={10} />
      </button>
    </div>
  );
}

function RenameRow({
  docId,
  oldName,
  onDone,
}: {
  docId: string;
  oldName: string;
  onDone: () => void;
}) {
  const [name, setName] = useState(oldName);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    if (trimmed === oldName) {
      onDone();
      return;
    }
    setBusy(true);
    const r = await call<{ ok: boolean; error?: string }>(
      "import_rename",
      docId,
      oldName,
      trimmed,
    );
    setBusy(false);
    if (!r.ok) {
      setErr(r.error || "could not rename");
      return;
    }
    onDone();
  };

  return (
    <div className="flex flex-col gap-1 rounded-sm bg-[var(--color-panel-2)] p-1.5">
      <div className="flex items-center gap-1">
        <FileBox size={11} className="shrink-0 text-[var(--color-muted)]" />
        <input
          autoFocus
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setErr(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            else if (e.key === "Escape") onDone();
          }}
          className="min-w-0 flex-1 rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs outline-none focus:border-[var(--color-focus)]"
        />
        <button
          onClick={submit}
          disabled={busy}
          title="Rename"
          className="rounded p-0.5 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)] disabled:opacity-30"
        >
          <Check size={11} />
        </button>
        <button
          onClick={onDone}
          title="Cancel"
          className="rounded p-0.5 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
        >
          <X size={11} />
        </button>
      </div>
      {err && <div className="text-[10px] text-[#f48771]">{err}</div>}
    </div>
  );
}

