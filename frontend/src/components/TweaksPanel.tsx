import { useEffect, useState } from "react";
import { call } from "@/lib/pywebview";
import { useDoc, type ActiveKind } from "@/lib/doc";

export function TweaksPanel() {
  const { doc } = useDoc();
  const params = doc?.params ?? {};
  const entries = Object.entries(params);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-3 py-2">
        <span className="text-xs uppercase tracking-wider text-[var(--color-muted)]">
          Parameters
        </span>
        {doc && (
          <ActiveBadge
            kind={doc.active_kind}
            objectName={doc.active_object}
            sketchName={doc.active_sketch}
          />
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {entries.length === 0 ? (
          <div className="rounded-sm border border-dashed border-[var(--color-border)] p-2 text-xs text-[var(--color-muted)]">
            no parameters yet — ask the agent to define some
          </div>
        ) : (
          <div className="space-y-1.5">
            {entries.map(([name, value]) => (
              <ParamRow key={name} name={name} value={Number(value)} docId={doc!.id} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function ActiveBadge({
  kind,
  objectName,
  sketchName,
}: {
  kind: ActiveKind;
  objectName: string;
  sketchName: string | null;
}) {
  const name = kind === "sketch" ? sketchName ?? "(none)" : objectName;
  const label = kind === "sketch" ? "sketch" : "object";
  return (
    <span
      className="truncate font-mono text-[10px] text-[var(--color-muted)]"
      title={`active ${label}: ${name}`}
    >
      <span
        className={
          kind === "sketch"
            ? "mr-1 rounded-sm bg-[#7dd3fc]/15 px-1 text-[#7dd3fc]"
            : "mr-1 rounded-sm bg-[var(--color-hover)] px-1"
        }
      >
        {label}
      </span>
      {name}
    </span>
  );
}

function ParamRow({ name, value, docId }: { name: string; value: number; docId: string }) {
  const [draft, setDraft] = useState<string>(String(value));

  // Re-sync when an external change (the agent calls set_parameter, or a
  // checkout brings new params) updates `value`.
  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  // Live commit — debounce 250ms after the last keystroke.
  useEffect(() => {
    const v = Number(draft);
    if (!Number.isFinite(v) || v === value) return;
    const t = setTimeout(() => {
      call("project_set_parameter", docId, name, v);
    }, 250);
    return () => clearTimeout(t);
  }, [draft, value, name, docId]);

  return (
    <div className="flex items-center gap-2 rounded-sm px-2 py-1 hover:bg-[var(--color-hover)]">
      <label className="flex-1 truncate font-mono text-xs text-[var(--color-text)]">{name}</label>
      <input
        type="number"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className="w-20 rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-0.5 text-right font-mono text-xs outline-none focus:border-[var(--color-focus)]"
      />
    </div>
  );
}
