import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Check, ListChecks, Pencil, Plus, Trash2, X } from "lucide-react";
import { Dialog, SecondaryButton, TextInput } from "./Dialog";
import { call } from "@/lib/pywebview";
import { cn } from "@/lib/utils";

type Props = {
  open: boolean;
  onClose: () => void;
  docId: string;
  objectName: string;
  /** Current persisted list — used to seed local state when the dialog opens. */
  initial: string[];
};

/**
 * Per-object requirements editor. Auto-saves on every mutation: each add /
 * edit / delete / reorder fires a write, so closing the dialog never loses
 * work. The list state is owned locally during the session — server updates
 * propagate via project_state but we ignore them so they don't clobber an
 * in-flight rename.
 */
export function RequirementsDialog({ open, onClose, docId, objectName, initial }: Props) {
  const [items, setItems] = useState<string[]>([]);
  const [draft, setDraft] = useState("");
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editingText, setEditingText] = useState("");

  useEffect(() => {
    if (!open) return;
    setItems(initial);
    setDraft("");
    setEditingIdx(null);
    setEditingText("");
  }, [open, initial]);

  const persist = async (next: string[]) => {
    setItems(next);
    await call("object_set_requirements", docId, objectName, next);
  };

  const addItem = async () => {
    const t = draft.trim();
    if (!t) return;
    setDraft("");
    await persist([...items, t]);
  };

  const removeAt = async (i: number) => {
    await persist(items.filter((_, j) => j !== i));
  };

  const move = async (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= items.length) return;
    const next = items.slice();
    [next[i], next[j]] = [next[j], next[i]];
    await persist(next);
  };

  const beginEdit = (i: number) => {
    setEditingIdx(i);
    setEditingText(items[i]);
  };

  const commitEdit = async () => {
    if (editingIdx === null) return;
    const t = editingText.trim();
    if (!t) {
      // Empty save → delete that row.
      const idx = editingIdx;
      setEditingIdx(null);
      setEditingText("");
      await removeAt(idx);
      return;
    }
    const idx = editingIdx;
    setEditingIdx(null);
    setEditingText("");
    if (t === items[idx]) return;
    const next = items.slice();
    next[idx] = t;
    await persist(next);
  };

  const cancelEdit = () => {
    setEditingIdx(null);
    setEditingText("");
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Requirements — ${objectName}`}
      width="w-[560px]"
      footer={<SecondaryButton onClick={onClose}>Close</SecondaryButton>}
    >
      <div className="flex flex-col gap-3">
        <p className="text-xs text-[var(--color-muted)]">
          Hard constraints the agent must satisfy on every change. Plain English is
          fine — the agent will verify with measurements after each edit.
        </p>

        {items.length === 0 ? (
          <div className="rounded-md border border-dashed border-[var(--color-border)] px-3 py-6 text-center text-xs text-[var(--color-muted)]">
            No requirements yet. Add one below.
          </div>
        ) : (
          <ol className="flex flex-col gap-1">
            {items.map((req, i) => (
              <li
                key={i}
                className="group flex items-start gap-2 rounded-sm border border-[var(--color-border)] bg-[var(--color-panel-2)] px-2 py-1.5 text-sm"
              >
                <span className="mt-0.5 w-5 shrink-0 text-right tabular-nums text-[var(--color-muted)]">
                  {i + 1}.
                </span>
                {editingIdx === i ? (
                  <div className="flex flex-1 items-center gap-1">
                    <TextInput
                      autoFocus
                      value={editingText}
                      onChange={(e) => setEditingText(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") commitEdit();
                        else if (e.key === "Escape") cancelEdit();
                      }}
                    />
                    <IconButton onClick={commitEdit} label="Save">
                      <Check size={12} />
                    </IconButton>
                    <IconButton onClick={cancelEdit} label="Cancel">
                      <X size={12} />
                    </IconButton>
                  </div>
                ) : (
                  <>
                    <span className="flex-1 break-words text-[var(--color-text)]">
                      {req}
                    </span>
                    <div className="flex shrink-0 items-center gap-0.5 opacity-50 group-hover:opacity-100">
                      <IconButton
                        onClick={() => move(i, -1)}
                        disabled={i === 0}
                        label="Move up"
                      >
                        <ArrowUp size={12} />
                      </IconButton>
                      <IconButton
                        onClick={() => move(i, 1)}
                        disabled={i === items.length - 1}
                        label="Move down"
                      >
                        <ArrowDown size={12} />
                      </IconButton>
                      <IconButton onClick={() => beginEdit(i)} label="Edit">
                        <Pencil size={12} />
                      </IconButton>
                      <IconButton onClick={() => removeAt(i)} label="Delete">
                        <Trash2 size={12} />
                      </IconButton>
                    </div>
                  </>
                )}
              </li>
            ))}
          </ol>
        )}

        <div className="flex items-center gap-2">
          <ListChecks size={14} className="shrink-0 text-[var(--color-muted)]" />
          <TextInput
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addItem();
            }}
            placeholder="e.g. inner wall thickness ≥ 2 mm"
          />
          <button
            onClick={addItem}
            disabled={!draft.trim()}
            className="flex h-8 items-center gap-1 rounded-sm bg-[var(--color-accent)] px-2 text-xs text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
          >
            <Plus size={12} />
            <span>Add</span>
          </button>
        </div>
      </div>
    </Dialog>
  );
}

function IconButton({
  children,
  onClick,
  disabled,
  label,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      aria-label={label}
      className={cn(
        "rounded-sm p-1 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]",
        disabled && "opacity-30",
      )}
    >
      {children}
    </button>
  );
}
