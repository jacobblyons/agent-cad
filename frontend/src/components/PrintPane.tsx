import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  Boxes,
  Camera,
  Loader2,
  Printer as PrinterIcon,
  RefreshCw,
  RotateCw,
  Send,
  Settings as SettingsIcon,
  Sparkles,
  Thermometer,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { call } from "@/lib/pywebview";
import {
  fmtGrams,
  fmtMinutes,
  type SliceOverride,
  usePrint,
} from "@/lib/print";

function fmtTemp(t: number | null | undefined): string {
  if (t == null) return "—";
  return `${t.toFixed(0)}°`;
}
import { useUi } from "@/lib/ui";
import { cn } from "@/lib/utils";

/**
 * PrintPane — takes over the viewer area in the print phase.
 *
 * Layout (top → bottom):
 *   - Header row: "Back to CAD" button, project / printer summary
 *   - Big "preset" segmented chooser (the only setting the user
 *     edits directly)
 *   - Slice estimate card (time + filament, agent overrides applied)
 *   - Override list (read-only display of agent-applied tweaks; user
 *     can clear or remove individuals)
 *   - Send-to-printer footer: target printer + Send button
 *
 * The chat panel (left side of the screen) stays wired to the agent —
 * the agent gets a phase-aware prompt block and a different tool
 * surface, so the user can keep talking to it inside the print phase.
 */
export function PrintPane() {
  const ui = useUi();
  const {
    session,
    printers,
    presets,
    busy,
    leave,
    setPreset,
    setPrinter,
    setOverrides,
    slice,
    send,
    refreshPrinterState,
  } = usePrint();

  const slice_ = session?.last_slice ?? null;
  const overrides = session?.overrides ?? [];
  const printerState = session?.printer_state ?? null;

  // Snapshot lives on the front-end only — backend doesn't cache the
  // JPEG. Camera fetch returns the path to a temp file; we read it as
  // a data URL once and hold it in component state until the next
  // capture or phase exit.
  const [snapshotDataUrl, setSnapshotDataUrl] = useState<string | null>(null);
  const [snapshotErr, setSnapshotErr] = useState<string | null>(null);
  const [snapshotBusy, setSnapshotBusy] = useState(false);
  const grabSnapshot = async () => {
    if (snapshotBusy) return;
    setSnapshotBusy(true);
    setSnapshotErr(null);
    try {
      const r = await call<{ ok: boolean; data_url?: string; error?: string }>(
        "print_camera_snapshot",
        session?.project_id ?? null,
      );
      if (r?.ok && r.data_url) {
        setSnapshotDataUrl(r.data_url);
      } else {
        setSnapshotErr(r?.error ?? "snapshot failed");
      }
    } finally {
      setSnapshotBusy(false);
    }
  };
  // Auto-poll progress + auto-take a snapshot every minute while the
  // printer reports an active job. Cheap (one MQTT round-trip + one
  // RTSPS frame) and gives the user a near-live view without them
  // having to click Refresh.
  useEffect(() => {
    if (!printerState?.online) return;
    if (printerState.gcode_state !== "RUNNING") return;
    const id = setInterval(() => {
      void refreshPrinterState();
    }, 30_000);
    return () => clearInterval(id);
    // We deliberately depend only on the bits that determine whether
    // we should poll, not the full printerState object, to avoid
    // resetting the interval every report.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [printerState?.online, printerState?.gcode_state]);

  const printer = useMemo(
    () => printers.find((p) => p.id === session?.printer_id) ?? null,
    [printers, session?.printer_id],
  );

  // Auto-detected filament + bed type, with fallback to the printer's
  // configured default_bed_type when MQTT didn't report a plate.
  const activeSlot = useMemo(() => {
    if (!printerState || !printerState.online) return null;
    if (printerState.active_tray < 0) {
      return printerState.slots[0] ?? null;
    }
    return (
      printerState.slots.find((s) => s.tray_id === printerState.active_tray) ??
      printerState.slots[0] ??
      null
    );
  }, [printerState]);
  const detectedBedTypeSlicer =
    printerState?.bed_type_slicer || printer?.default_bed_type || "";

  const removeOverride = (key: string) => {
    setOverrides(overrides.filter((o) => o.key !== key));
  };
  const clearOverrides = () => setOverrides([]);

  return (
    <div className="absolute inset-0 flex min-h-0 flex-col bg-[var(--color-bg)] text-[var(--color-text)]">
      {/* header */}
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-panel-2)] px-3">
        <button
          onClick={leave}
          title="Back to CAD"
          className="flex h-8 items-center gap-1.5 rounded-sm border border-[var(--color-border)] px-2.5 text-xs text-[var(--color-text)] hover:bg-[var(--color-hover)]"
        >
          <ArrowLeft size={12} />
          <span>Back to CAD</span>
        </button>
        <span className="ml-2 flex items-center gap-1.5 text-xs text-[var(--color-muted)]">
          <PrinterIcon size={12} />
          <span>Print phase</span>
        </span>
        <div className="ml-auto flex items-center gap-2">
          {printers.length === 0 ? (
            <span className="text-xs text-[#dcb073]">
              No printers configured —{" "}
              <button
                onClick={ui.openSettings}
                className="underline underline-offset-2 hover:text-[var(--color-text)]"
              >
                add one
              </button>
            </span>
          ) : (
            <select
              value={session?.printer_id ?? ""}
              onChange={(e) => setPrinter(e.target.value)}
              className="h-7 rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-2 text-xs text-[var(--color-text)] outline-none focus:border-[var(--color-focus)]"
            >
              {printers.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          <button
            onClick={ui.openSettings}
            title="Settings"
            className="flex h-7 w-7 items-center justify-center rounded-sm text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
          >
            <SettingsIcon size={13} />
          </button>
        </div>
      </div>

      {/* body */}
      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        <div className="mx-auto max-w-[680px] space-y-6">
          {/* preset chooser */}
          <section>
            <h2 className="mb-2 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
              Preset
            </h2>
            <div className="grid grid-cols-3 gap-2">
              {presets.map((p) => {
                const active = session?.preset === p.id;
                return (
                  <button
                    key={p.id}
                    onClick={() => setPreset(p.id)}
                    disabled={busy}
                    className={cn(
                      "rounded-md border px-3 py-2 text-left transition disabled:opacity-50",
                      active
                        ? "border-[var(--color-accent)] bg-[var(--color-selection)] text-[var(--color-text)]"
                        : "border-[var(--color-border)] bg-[var(--color-panel)] hover:border-[var(--color-focus)]",
                    )}
                  >
                    <div className="text-sm font-medium">{p.label}</div>
                    <div className="mt-1 text-[11px] text-[var(--color-muted)]">
                      {p.description}
                    </div>
                  </button>
                );
              })}
            </div>
          </section>

          {/* live printer snapshot */}
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
                <Boxes size={11} />
                Live from printer
              </h2>
              <button
                onClick={refreshPrinterState}
                disabled={busy}
                title="Re-query the printer over MQTT"
                className="flex h-7 items-center gap-1.5 rounded-sm border border-[var(--color-border)] px-2.5 text-[11px] hover:bg-[var(--color-hover)] disabled:opacity-50"
              >
                {busy ? (
                  <Loader2 size={11} className="animate-spin" />
                ) : (
                  <RefreshCw size={11} />
                )}
                <span>Refresh</span>
              </button>
            </div>
            {printerState === null ? (
              <div className="rounded-md border border-dashed border-[var(--color-border)] px-4 py-3 text-center text-xs text-[var(--color-muted)]">
                {busy
                  ? "Querying printer over MQTT…"
                  : "No live state yet — click Refresh to query the printer."}
              </div>
            ) : !printerState.online ? (
              <div className="rounded-md border border-[#dcb073] bg-[var(--color-panel)] p-3 text-xs text-[#dcb073]">
                <div className="mb-1 font-medium">Printer didn't respond</div>
                <div className="text-[var(--color-muted)]">
                  {printerState.error ||
                    "MQTT query timed out. Confirm Developer / LAN-Only Mode is on and the access code matches."}
                </div>
              </div>
            ) : (
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] p-4">
                <div className="grid grid-cols-3 gap-4">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Filament
                    </div>
                    <div className="mt-1 flex items-center gap-2">
                      {activeSlot?.color_hex && (
                        <span
                          className="inline-block h-3 w-3 shrink-0 rounded-sm border border-[var(--color-border)]"
                          style={{
                            backgroundColor: `#${activeSlot.color_hex.slice(0, 6)}`,
                          }}
                        />
                      )}
                      <span className="font-mono text-sm text-[var(--color-text)]">
                        {activeSlot?.type ?? "—"}
                      </span>
                    </div>
                    {activeSlot?.sub_brand && (
                      <div className="text-[11px] text-[var(--color-muted)]">
                        {activeSlot.sub_brand}
                      </div>
                    )}
                    <div className="text-[10px] text-[var(--color-muted)]">
                      tray{" "}
                      {printerState.active_tray === 254
                        ? "external"
                        : printerState.active_tray}
                      {activeSlot?.tray_info_idx
                        ? ` · ${activeSlot.tray_info_idx}`
                        : ""}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Build plate
                    </div>
                    <div className="mt-1 font-mono text-sm text-[var(--color-text)]">
                      {detectedBedTypeSlicer || "—"}
                    </div>
                    <div className="text-[10px] text-[var(--color-muted)]">
                      {printerState.bed_type_slicer
                        ? "from printer"
                        : "from config (firmware doesn't report)"}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Nozzle
                    </div>
                    <div className="mt-1 font-mono text-sm text-[var(--color-text)]">
                      {printerState.nozzle_diameter_mm
                        ? `${printerState.nozzle_diameter_mm.toFixed(1)} mm`
                        : "—"}
                    </div>
                    {printerState.nozzle_type && (
                      <div className="text-[10px] text-[var(--color-muted)]">
                        {printerState.nozzle_type}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </section>

          {/* now printing — only renders when there's an active job */}
          {printerState?.online && printerState.gcode_state === "RUNNING" && (
            <section>
              <div className="mb-2 flex items-center justify-between">
                <h2 className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
                  <Thermometer size={11} />
                  Now printing
                </h2>
              </div>
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] p-4 space-y-3">
                {/* progress bar + numbers */}
                <div>
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="truncate text-[var(--color-muted)]">
                      {printerState.print_filename || "(unnamed)"}
                    </span>
                    <span className="ml-2 font-mono tabular-nums text-[var(--color-text)]">
                      {printerState.progress_pct ?? 0}%
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-sm bg-[var(--color-hover)]">
                    <div
                      className="h-full bg-[var(--color-accent)] transition-all"
                      style={{
                        width: `${Math.max(0, Math.min(100, printerState.progress_pct ?? 0))}%`,
                      }}
                    />
                  </div>
                  <div className="mt-1 flex justify-between text-[11px] text-[var(--color-muted)]">
                    <span>
                      {printerState.layer_num != null && printerState.total_layer_num
                        ? `layer ${printerState.layer_num} / ${printerState.total_layer_num}`
                        : ""}
                    </span>
                    <span>
                      {printerState.time_remaining_min != null
                        ? `${
                            Math.floor(printerState.time_remaining_min / 60)
                          }h ${printerState.time_remaining_min % 60}m left`
                        : ""}
                    </span>
                  </div>
                </div>
                {/* temps */}
                <div className="grid grid-cols-3 gap-3 text-center">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Nozzle
                    </div>
                    <div className="font-mono text-sm text-[var(--color-text)]">
                      {fmtTemp(printerState.nozzle_c)} /{" "}
                      <span className="text-[var(--color-muted)]">
                        {fmtTemp(printerState.nozzle_target_c)}
                      </span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Bed
                    </div>
                    <div className="font-mono text-sm text-[var(--color-text)]">
                      {fmtTemp(printerState.bed_c)} /{" "}
                      <span className="text-[var(--color-muted)]">
                        {fmtTemp(printerState.bed_target_c)}
                      </span>
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Chamber
                    </div>
                    <div className="font-mono text-sm text-[var(--color-text)]">
                      {fmtTemp(printerState.chamber_c)}
                    </div>
                  </div>
                </div>
              </div>
            </section>
          )}

          {/* camera frame */}
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
                <Camera size={11} />
                Camera
              </h2>
              <button
                onClick={grabSnapshot}
                disabled={snapshotBusy || !printer?.has_access_code}
                title="Grab one frame from the printer's chamber camera"
                className="flex h-7 items-center gap-1.5 rounded-sm border border-[var(--color-border)] px-2.5 text-[11px] hover:bg-[var(--color-hover)] disabled:opacity-50"
              >
                {snapshotBusy ? (
                  <Loader2 size={11} className="animate-spin" />
                ) : (
                  <Camera size={11} />
                )}
                <span>{snapshotDataUrl ? "Refresh" : "Capture"}</span>
              </button>
            </div>
            {snapshotErr ? (
              <div className="rounded-md border border-[#f48771] bg-[#3a1d1d] p-3 text-xs text-[#f48771]">
                {snapshotErr}
              </div>
            ) : snapshotDataUrl ? (
              <img
                src={snapshotDataUrl}
                alt="Printer camera"
                className="w-full rounded-md border border-[var(--color-border)]"
              />
            ) : (
              <div className="rounded-md border border-dashed border-[var(--color-border)] px-4 py-6 text-center text-xs text-[var(--color-muted)]">
                {snapshotBusy
                  ? "Grabbing camera frame…"
                  : "Click Capture to pull one frame from the printer's chamber camera."}
              </div>
            )}
          </section>

          {/* slice estimate */}
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
                Slice estimate
              </h2>
              <button
                onClick={slice}
                disabled={busy}
                title="Re-slice with the current preset and overrides"
                className="flex h-7 items-center gap-1.5 rounded-sm border border-[var(--color-border)] px-2.5 text-[11px] hover:bg-[var(--color-hover)] disabled:opacity-50"
              >
                {busy ? (
                  <Loader2 size={11} className="animate-spin" />
                ) : (
                  <RotateCw size={11} />
                )}
                <span>{busy ? "Slicing…" : "Re-slice"}</span>
              </button>
            </div>

            {slice_ === null ? (
              <div className="rounded-md border border-dashed border-[var(--color-border)] px-4 py-6 text-center text-xs text-[var(--color-muted)]">
                {busy
                  ? "Slicing — this can take a moment for large parts…"
                  : "No slice yet. Click Re-slice or change the preset to begin."}
              </div>
            ) : slice_.ok ? (
              <div className="rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] p-4">
                <div className="grid grid-cols-3 gap-4 text-center">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Print time
                    </div>
                    <div className="font-mono text-lg text-[var(--color-text)]">
                      {fmtMinutes(slice_.estimated_minutes)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Filament
                    </div>
                    <div className="font-mono text-lg text-[var(--color-text)]">
                      {fmtGrams(slice_.estimated_filament_g)}
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                      Format
                    </div>
                    <div className="font-mono text-lg text-[var(--color-text)]">
                      {slice_.sliced_format ?? "—"}
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="rounded-md border border-[#f48771] bg-[#3a1d1d] p-3 text-xs text-[#f48771]">
                <div className="mb-1 flex items-center gap-1.5 font-medium">
                  <TriangleAlert size={12} />
                  Slice failed
                </div>
                <div className="whitespace-pre-wrap break-all">
                  {slice_.error ?? "unknown error"}
                </div>
              </div>
            )}
          </section>

          {/* overrides */}
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
                <Sparkles size={11} />
                Agent overrides
              </h2>
              {overrides.length > 0 && (
                <button
                  onClick={clearOverrides}
                  disabled={busy}
                  className="text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-text)] disabled:opacity-50"
                >
                  Clear all
                </button>
              )}
            </div>
            {overrides.length === 0 ? (
              <div className="rounded-md border border-dashed border-[var(--color-border)] px-4 py-3 text-center text-xs text-[var(--color-muted)]">
                Preset defaults only. The agent can apply overrides via chat
                (eg. enable supports, raise infill).
              </div>
            ) : (
              <ul className="space-y-1.5">
                {overrides.map((o: SliceOverride) => (
                  <li
                    key={o.key}
                    className="flex items-start gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-panel)] px-3 py-2"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2 font-mono text-xs">
                        <span className="text-[var(--color-accent)]">
                          {o.key}
                        </span>
                        <span className="text-[var(--color-muted)]">=</span>
                        <span className="text-[var(--color-text)]">
                          {o.value}
                        </span>
                      </div>
                      {o.note && (
                        <div className="mt-0.5 text-[11px] text-[var(--color-muted)]">
                          {o.note}
                        </div>
                      )}
                    </div>
                    <button
                      onClick={() => removeOverride(o.key)}
                      disabled={busy}
                      title="Remove this override"
                      className="rounded-sm p-1 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[#f48771] disabled:opacity-50"
                    >
                      <Trash2 size={11} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      </div>

      {/* footer: send */}
      <div className="flex h-14 shrink-0 items-center gap-3 border-t border-[var(--color-border)] bg-[var(--color-panel-2)] px-4">
        <div className="min-w-0 flex-1 truncate text-xs text-[var(--color-muted)]">
          {session?.last_send_message ? (
            <span
              className={
                session.last_send_ok
                  ? "text-[#89d185]"
                  : session.last_send_ok === false
                    ? "text-[#f48771]"
                    : ""
              }
            >
              {session.last_send_message}
            </span>
          ) : printer ? (
            <>
              Will send to <span className="text-[var(--color-text)]">{printer.name}</span>
              {printer.ip && (
                <>
                  {" "}
                  at <span className="font-mono">{printer.ip}</span>
                </>
              )}
              {!printer.has_access_code && (
                <span className="ml-2 text-[#dcb073]">
                  (missing access code)
                </span>
              )}
            </>
          ) : (
            "Pick a printer to send."
          )}
        </div>
        <button
          onClick={send}
          disabled={
            busy ||
            !slice_?.ok ||
            !printer ||
            !printer.has_access_code
          }
          className="flex h-9 items-center gap-2 rounded-sm bg-[var(--color-accent)] px-4 text-sm text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
        >
          <Send size={13} />
          <span>Send to printer</span>
        </button>
      </div>
    </div>
  );
}
