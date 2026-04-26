import { useState } from "react";
import {
  Check,
  Eye,
  EyeOff,
  Pencil,
  PenLine,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { call } from "@/lib/pywebview";
import { useDoc } from "@/lib/doc";
import { useChat } from "@/lib/chat";
import { cn } from "@/lib/utils";

/**
 * Mirror of ObjectBrowser for sketches. Sketches don't have requirements
 * (yet); the row is otherwise the same: visibility toggle, click to set
 * active, rename, delete.
 *
 * Visual cue that distinguishes sketches from objects: the PenLine icon
 * and a tinted "active" highlight that signals "active edit target is a
 * sketch right now" so the user knows their next prompt will edit a 2D
 * profile, not a 3D model.
 */
export function SketchBrowser() {
  const { doc } = useDoc();
  const { isAgentRunning } = useChat();
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);

  if (!doc) return null;
  const sketches = doc.sketches ?? [];
  const activeKind = doc.active_kind;
  const activeSketch = doc.active_sketch ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
        <span className="text-xs uppercase tracking-wider text-[var(--color-muted)]">
          Sketches
        </span>
        <button
          onClick={() => setCreating(true)}
          disabled={isAgentRunning}
          title="New sketch"
          className="flex h-5 w-5 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)] disabled:opacity-40"
        >
          <Plus size={12} />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {sketches.length === 0 && !creating && (
          <div className="px-2 py-2 text-[11px] leading-relaxed text-[var(--color-muted)]">
            No sketches yet. Sketches are 2D profiles other parts can extrude
            from. Ask the agent or click + to start one.
          </div>
        )}
        <div className="space-y-0.5">
          {sketches.map((s) =>
            renaming === s.name ? (
              <RenameRow
                key={s.name}
                docId={doc.id}
                oldName={s.name}
                onDone={() => setRenaming(null)}
              />
            ) : (
              <SketchRow
                key={s.name}
                docId={doc.id}
                name={s.name}
                active={activeKind === "sketch" && activeSketch === s.name}
                visible={s.visible}
                disabled={isAgentRunning}
                onRename={() => setRenaming(s.name)}
              />
            ),
          )}
          {creating && (
            <CreateRow
              docId={doc.id}
              existing={sketches.map((s) => s.name)}
              onDone={() => setCreating(false)}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function SketchRow({
  docId,
  name,
  active,
  visible,
  disabled,
  onRename,
}: {
  docId: string;
  name: string;
  active: boolean;
  visible: boolean;
  disabled: boolean;
  onRename: () => void;
}) {
  const setActive = async () => {
    if (active || disabled) return;
    await call("sketch_set_active", docId, name);
  };
  const toggleVisible = async () => {
    if (disabled) return;
    await call("sketch_set_visible", docId, name, !visible);
  };
  const del = async () => {
    if (disabled) return;
    if (
      !window.confirm(
        `Delete sketch "${name}"? Object scripts that consume it will break until updated.`,
      )
    )
      return;
    await call("sketch_delete", docId, name);
  };
  const VisIcon = visible ? Eye : EyeOff;
  return (
    <div
      role="button"
      onClick={setActive}
      className={cn(
        "group flex items-center gap-1.5 rounded-sm px-2 py-1 text-xs",
        active
          ? "bg-[var(--color-selection)] text-[var(--color-text)]"
          : "text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
        disabled && "opacity-50",
        !disabled && !active && "cursor-pointer",
      )}
    >
      <button
        onClick={(e) => {
          e.stopPropagation();
          toggleVisible();
        }}
        disabled={disabled}
        title={visible ? "Hide overlay in viewer" : "Show overlay in viewer"}
        className={cn(
          "rounded p-0.5 transition hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
          visible
            ? "opacity-0 group-hover:opacity-60 hover:!opacity-100"
            : "opacity-100 text-[var(--color-muted)]",
          disabled && "opacity-30",
        )}
      >
        <VisIcon size={11} />
      </button>
      <PenLine size={11} className={cn("shrink-0", !visible && "opacity-40")} />
      <span className={cn("flex-1 truncate font-mono", !visible && "opacity-50")}>
        {name}
      </span>
      <button
        onClick={(e) => {
          e.stopPropagation();
          onRename();
        }}
        disabled={disabled}
        title="Rename"
        className="rounded p-0.5 opacity-0 transition group-hover:opacity-60 hover:bg-[var(--color-hover)] hover:!opacity-100 disabled:opacity-0"
      >
        <Pencil size={10} />
      </button>
      <button
        onClick={(e) => {
          e.stopPropagation();
          del();
        }}
        disabled={disabled}
        title="Delete"
        className="rounded p-0.5 opacity-0 transition group-hover:opacity-60 hover:bg-[var(--color-hover)] hover:!opacity-100 disabled:opacity-0"
      >
        <Trash2 size={10} />
      </button>
    </div>
  );
}

function CreateRow({
  docId,
  existing,
  onDone,
}: {
  docId: string;
  existing: string[];
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    if (existing.includes(trimmed)) {
      setErr(`'${trimmed}' already exists`);
      return;
    }
    setBusy(true);
    const r = await call<{ ok: boolean; error?: string }>(
      "sketch_create",
      docId,
      trimmed,
    );
    setBusy(false);
    if (!r.ok) {
      setErr(r.error || "could not create sketch");
      return;
    }
    onDone();
  };

  return (
    <div className="flex flex-col gap-1 rounded-sm bg-[var(--color-panel-2)] p-1.5">
      <div className="flex items-center gap-1">
        <PenLine size={11} className="shrink-0 text-[var(--color-muted)]" />
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
          placeholder="sketch-name"
          className="min-w-0 flex-1 rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-0.5 font-mono text-xs outline-none focus:border-[var(--color-focus)]"
        />
        <button
          onClick={submit}
          disabled={busy || !name.trim()}
          title="Create"
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
      "sketch_rename",
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
        <PenLine size={11} className="shrink-0 text-[var(--color-muted)]" />
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
