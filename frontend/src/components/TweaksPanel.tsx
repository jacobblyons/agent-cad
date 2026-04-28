import { useEffect, useState } from "react";
import { call } from "@/lib/pywebview";
import { useDoc, type ActiveKind } from "@/lib/doc";
import { useUnit, type Unit } from "@/lib/units";

const MM_PER_INCH = 25.4;

/** Heuristic: is this parameter likely a length (mm)?
 *
 * Backend params are bare numbers — there's no unit metadata. Most CAD
 * params are lengths, with a few obvious exceptions (angles, counts,
 * ratios) we name-detect. False positives are tolerable: user just sees
 * an "in" toggle that converts a degree value, and can flip back.
 *
 * The patterns are deliberately conservative — we only opt OUT of length
 * treatment for names that explicitly mention a non-length quantity.
 * `lead_in_relief` (contains "in") stays a length. */
function isLengthParam(name: string): boolean {
  const n = name.toLowerCase();
  if (n.endsWith("_deg") || n.includes("angle")) return false;
  if (n.includes("ratio")) return false;
  if (n === "count" || n === "n") return false;
  if (n.endsWith("_count") || n.startsWith("count_")) return false;
  if (n.endsWith("_n") || n.startsWith("n_")) return false;
  return true;
}

function formatParamValue(value: number, unit: Unit, isLength: boolean): string {
  if (!isLength) return String(value);
  if (unit === "in") {
    const v = value / MM_PER_INCH;
    return Math.abs(v) < 1 ? v.toFixed(3) : v.toFixed(2);
  }
  return Math.abs(value) < 10 ? value.toFixed(2) : value.toFixed(1);
}

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
  const unit = useUnit();
  const isLength = isLengthParam(name);
  // The MM equivalent of whatever the user has typed; commits go to the
  // backend in mm regardless of display unit.
  const toMm = (input: string): number => {
    const n = Number(input);
    if (!Number.isFinite(n)) return NaN;
    return isLength && unit === "in" ? n * MM_PER_INCH : n;
  };

  const [draft, setDraft] = useState<string>(() => formatParamValue(value, unit, isLength));

  // Re-sync when the underlying mm value or display unit changes — but
  // only if the new display string would represent a different mm value
  // than the user's current draft. Otherwise our own commit echoing back
  // (or a unit toggle that produces the same number to the precision we
  // display) would clobber what they're typing.
  useEffect(() => {
    const formatted = formatParamValue(value, unit, isLength);
    const draftMm = toMm(draft);
    if (Number.isFinite(draftMm) && Math.abs(draftMm - value) < 1e-4) return;
    setDraft(formatted);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, unit, isLength]);

  // Live commit — debounce 250ms after the last keystroke. Convert to mm
  // before sending; the backend stays mm-only.
  useEffect(() => {
    const mm = toMm(draft);
    if (!Number.isFinite(mm) || Math.abs(mm - value) < 1e-6) return;
    const t = setTimeout(() => {
      call("project_set_parameter", docId, name, mm);
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, value, name, docId, unit, isLength]);

  return (
    <div className="flex items-center gap-2 rounded-sm px-2 py-1 hover:bg-[var(--color-hover)]">
      <label className="flex-1 truncate font-mono text-xs text-[var(--color-text)]">{name}</label>
      <input
        type="number"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className="w-20 rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-1.5 py-0.5 text-right font-mono text-xs outline-none focus:border-[var(--color-focus)]"
      />
      {isLength && (
        <span className="w-5 shrink-0 font-mono text-[10px] text-[var(--color-muted)]">
          {unit}
        </span>
      )}
    </div>
  );
}
