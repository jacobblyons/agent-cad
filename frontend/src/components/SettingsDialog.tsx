import { useEffect, useState } from "react";
import { FolderOpen, Plus, Trash2 } from "lucide-react";
import { call } from "@/lib/pywebview";
import { Dialog, FieldLabel, PrimaryButton, SecondaryButton, TextInput } from "./Dialog";

type PrinterConfig = {
  id: string;
  name: string;
  kind: string;
  ip: string;
  serial: string;
  access_code: string;
  printer_profile: string;
  process_profile: string;
  filament_profile: string;
};

type Settings = {
  model: string;
  default_project_dir: string;
  effort: string;
  sketchfab_enabled: boolean;
  sketchfab_token: string;
  playwright_enabled: boolean;
  playwright_require_permission: boolean;
  printers: PrinterConfig[];
  default_printer_id: string;
  bambu_studio_cli_path: string;
};
type ModelOpt = { id: string; label: string; tier: string };
type EffortOpt = { id: string; label: string };
type GetResponse = {
  ok: boolean;
  settings: Settings;
  models: ModelOpt[];
  efforts: EffortOpt[];
};

function emptyBambuPrinter(): PrinterConfig {
  return {
    id: `bambu-${Math.random().toString(36).slice(2, 8)}`,
    name: "Bambu X1C",
    kind: "bambu_x1c",
    ip: "",
    serial: "",
    access_code: "",
    printer_profile: "",
    process_profile: "",
    filament_profile: "",
  };
}

type Props = { open: boolean; onClose: () => void };

export function SettingsDialog({ open, onClose }: Props) {
  const [draft, setDraft] = useState<Settings | null>(null);
  const [models, setModels] = useState<ModelOpt[]>([]);
  const [efforts, setEfforts] = useState<EffortOpt[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    (async () => {
      const r = await call<GetResponse>("settings_get");
      if (r?.ok) {
        setDraft(r.settings);
        setModels(r.models);
        setEfforts(r.efforts);
      }
    })();
  }, [open]);

  const pickDir = async () => {
    const r = await call<{ ok: boolean; path?: string; cancelled?: boolean }>("pick_directory");
    if (r?.ok && r.path && draft) {
      setDraft({ ...draft, default_project_dir: r.path });
    }
  };

  const submit = async () => {
    if (!draft) return;
    setBusy(true);
    const r = await call<{ ok: boolean; error?: string }>("settings_set", draft);
    setBusy(false);
    if (!r.ok) {
      setError(r.error || "could not save settings");
      return;
    }
    onClose();
  };

  return (
    <Dialog
      open={open}
      title="Settings"
      onClose={onClose}
      footer={
        <>
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
          <PrimaryButton onClick={submit} disabled={busy || !draft}>
            {busy ? "Saving…" : "Save"}
          </PrimaryButton>
        </>
      }
    >
      {!draft ? (
        <div className="text-xs text-[var(--color-muted)]">loading…</div>
      ) : (
        <>
          <div className="mb-4">
            <FieldLabel>Claude model</FieldLabel>
            <select
              value={draft.model}
              onChange={(e) => setDraft({ ...draft, model: e.target.value })}
              className="w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm text-[var(--color-text)] outline-none focus:border-[var(--color-focus)]"
            >
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label} — {m.tier}
                </option>
              ))}
            </select>
          </div>

          <div className="mb-4">
            <FieldLabel>Reasoning effort</FieldLabel>
            <select
              value={draft.effort}
              onChange={(e) => setDraft({ ...draft, effort: e.target.value })}
              className="w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm text-[var(--color-text)] outline-none focus:border-[var(--color-focus)]"
            >
              {efforts.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
            <p className="mt-1 text-xs text-[var(--color-muted)]">
              Lower = faster turns. Higher = the agent thinks more before acting and
              double-checks its work.
            </p>
          </div>

          <div>
            <FieldLabel>Default project directory</FieldLabel>
            <div className="flex gap-2">
              <TextInput
                value={draft.default_project_dir}
                onChange={(e) =>
                  setDraft({ ...draft, default_project_dir: e.target.value })
                }
              />
              <SecondaryButton onClick={pickDir} title="Browse for folder">
                <FolderOpen size={12} />
              </SecondaryButton>
            </div>
            <p className="mt-1 text-xs text-[var(--color-muted)]">
              Where new projects get created. Existing projects keep their original
              location.
            </p>
          </div>

          <div className="mt-4 border-t border-[var(--color-border)] pt-4">
            <label className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={draft.sketchfab_enabled}
                onChange={(e) =>
                  setDraft({ ...draft, sketchfab_enabled: e.target.checked })
                }
                className="h-3.5 w-3.5 cursor-pointer"
              />
              <span className="text-sm text-[var(--color-text)]">
                Enable Sketchfab integration
              </span>
            </label>
            <p className="mt-1 text-xs text-[var(--color-muted)]">
              Lets the agent search Sketchfab for reference parts, view
              previews, and download STEP files into the project's imports.
              Requires a personal API token from{" "}
              <span className="font-mono">sketchfab.com/settings/password</span>.
              The token stays on disk in your settings file and is only sent
              to sketchfab.com.
            </p>
            {draft.sketchfab_enabled && (
              <div className="mt-2">
                <FieldLabel>Sketchfab API token</FieldLabel>
                <TextInput
                  type="password"
                  value={draft.sketchfab_token}
                  onChange={(e) =>
                    setDraft({ ...draft, sketchfab_token: e.target.value })
                  }
                  placeholder="paste your token here"
                  spellCheck={false}
                />
              </div>
            )}
          </div>

          <div className="mt-4 border-t border-[var(--color-border)] pt-4">
            <label className="flex cursor-pointer items-center gap-2">
              <input
                type="checkbox"
                checked={draft.playwright_enabled}
                onChange={(e) =>
                  setDraft({ ...draft, playwright_enabled: e.target.checked })
                }
                className="h-3.5 w-3.5 cursor-pointer"
              />
              <span className="text-sm text-[var(--color-text)]">
                Enable Playwright browser{" "}
                <span className="ml-1 rounded-sm bg-[var(--color-hover)] px-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                  experimental
                </span>
              </span>
            </label>
            <p className="mt-1 text-xs text-[var(--color-muted)]">
              Spawns <span className="font-mono">@playwright/mcp</span> via npx so
              the agent can navigate real websites — useful for product configurators,
              login-walled datasheets, and pages WebFetch can't render. Requires
              Node.js on PATH; first run downloads its own Chromium build.
            </p>
            {draft.playwright_enabled && (
              <label className="mt-2 flex cursor-pointer items-start gap-2">
                <input
                  type="checkbox"
                  checked={draft.playwright_require_permission}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      playwright_require_permission: e.target.checked,
                    })
                  }
                  className="mt-0.5 h-3.5 w-3.5 cursor-pointer"
                />
                <span className="text-sm text-[var(--color-text)]">
                  Ask before each browser action
                  <span className="block text-xs text-[var(--color-muted)]">
                    Show a permission card in the chat for every Playwright
                    tool call. Turn this off to let the agent navigate freely.
                  </span>
                </span>
              </label>
            )}
          </div>

          <PrintersSection
            draft={draft}
            onChange={setDraft}
          />

          {error && (
            <div className="mt-3 rounded-sm border border-[#f48771] bg-[#3a1d1d] px-2 py-1.5 text-xs text-[#f48771]">
              {error}
            </div>
          )}
        </>
      )}
    </Dialog>
  );
}

function PrintersSection({
  draft,
  onChange,
}: {
  draft: Settings;
  onChange: (s: Settings) => void;
}) {
  const [testResult, setTestResult] = useState<Record<string, string>>({});
  const printers = draft.printers ?? [];

  const update = (idx: number, patch: Partial<PrinterConfig>) => {
    const next = printers.map((p, i) => (i === idx ? { ...p, ...patch } : p));
    onChange({ ...draft, printers: next });
  };
  const remove = (idx: number) => {
    const removed = printers[idx];
    const next = printers.filter((_, i) => i !== idx);
    let dflt = draft.default_printer_id;
    if (removed && removed.id === dflt) dflt = next[0]?.id ?? "";
    onChange({ ...draft, printers: next, default_printer_id: dflt });
  };
  const add = () => {
    const fresh = emptyBambuPrinter();
    const next = [...printers, fresh];
    onChange({
      ...draft,
      printers: next,
      default_printer_id: draft.default_printer_id || fresh.id,
    });
  };

  const test = async (idx: number) => {
    const p = printers[idx];
    if (!p) return;
    // Save the in-progress draft first so the backend can see it.
    setTestResult((r) => ({ ...r, [p.id]: "saving…" }));
    const save = await call<{ ok: boolean; error?: string }>(
      "settings_set",
      draft,
    );
    if (!save.ok) {
      setTestResult((r) => ({ ...r, [p.id]: `save failed: ${save.error}` }));
      return;
    }
    setTestResult((r) => ({ ...r, [p.id]: "testing…" }));
    const r = await call<{ ok: boolean; message?: string; error?: string }>(
      "print_test_printer",
      p.id,
    );
    setTestResult((cur) => ({
      ...cur,
      [p.id]: r.ok
        ? `✓ ${r.message ?? "reachable"}`
        : `✗ ${r.message ?? r.error ?? "unreachable"}`,
    }));
  };

  return (
    <div className="mt-4 border-t border-[var(--color-border)] pt-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm text-[var(--color-text)]">3D printers</span>
        <SecondaryButton onClick={add} title="Add a Bambu Lab X1C">
          <span className="flex items-center gap-1">
            <Plus size={11} />
            Add Bambu X1C
          </span>
        </SecondaryButton>
      </div>
      <p className="mb-3 text-xs text-[var(--color-muted)]">
        Configure at least one printer to enable the print phase. The X1C
        must be in <span className="font-mono">Developer Mode</span> for LAN
        access — find it under Settings → General → Developer Mode on the
        printer's screen. The access code lives under Settings → WLAN → ⓘ.
      </p>

      {printers.length === 0 && (
        <div className="rounded-sm border border-dashed border-[var(--color-border)] px-3 py-3 text-xs text-[var(--color-muted)]">
          No printers configured. Add one to unlock the print phase.
        </div>
      )}

      {printers.map((p, i) => {
        const isDefault = draft.default_printer_id === p.id;
        const status = testResult[p.id];
        return (
          <div
            key={p.id}
            className="mb-3 rounded-md border border-[var(--color-border)] bg-[var(--color-bg)] p-3"
          >
            <div className="mb-2 flex items-center gap-2">
              <TextInput
                value={p.name}
                onChange={(e) => update(i, { name: e.target.value })}
                placeholder="My X1C"
                className="!w-[180px]"
              />
              <span className="rounded-sm bg-[var(--color-hover)] px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
                {p.kind}
              </span>
              <label className="ml-auto flex cursor-pointer items-center gap-1 text-xs text-[var(--color-muted)]">
                <input
                  type="radio"
                  name="default-printer"
                  checked={isDefault}
                  onChange={() =>
                    onChange({ ...draft, default_printer_id: p.id })
                  }
                  className="h-3 w-3 cursor-pointer"
                />
                <span>Default</span>
              </label>
              <button
                onClick={() => remove(i)}
                title="Remove printer"
                className="rounded-sm p-1 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[#f48771]"
              >
                <Trash2 size={12} />
              </button>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div>
                <FieldLabel>IP address</FieldLabel>
                <TextInput
                  value={p.ip}
                  onChange={(e) => update(i, { ip: e.target.value })}
                  placeholder="192.168.1.42"
                  spellCheck={false}
                />
              </div>
              <div>
                <FieldLabel>Serial number</FieldLabel>
                <TextInput
                  value={p.serial}
                  onChange={(e) => update(i, { serial: e.target.value })}
                  placeholder="01S00A…"
                  spellCheck={false}
                />
              </div>
              <div className="col-span-2">
                <FieldLabel>Access code (developer mode)</FieldLabel>
                <TextInput
                  type="password"
                  value={p.access_code}
                  onChange={(e) => update(i, { access_code: e.target.value })}
                  placeholder="8 digits from the printer's screen"
                  spellCheck={false}
                />
              </div>
              <div className="col-span-2">
                <FieldLabel>Bambu Studio printer profile (optional)</FieldLabel>
                <TextInput
                  value={p.printer_profile}
                  onChange={(e) =>
                    update(i, { printer_profile: e.target.value })
                  }
                  placeholder="Bambu Lab X1 Carbon 0.4 nozzle"
                  spellCheck={false}
                />
                <p className="mt-1 text-[11px] text-[var(--color-muted)]">
                  Leave blank to use Bambu Studio's autodetect. Set to a
                  specific profile name from Bambu Studio if you've customised
                  one.
                </p>
              </div>
            </div>

            <div className="mt-2 flex items-center gap-2">
              <SecondaryButton onClick={() => test(i)}>
                Test connection
              </SecondaryButton>
              {status && (
                <span
                  className={
                    status.startsWith("✓")
                      ? "text-xs text-[#89d185]"
                      : status.startsWith("✗")
                        ? "text-xs text-[#f48771]"
                        : "text-xs text-[var(--color-muted)]"
                  }
                >
                  {status}
                </span>
              )}
            </div>
          </div>
        );
      })}

      <div className="mt-3">
        <FieldLabel>Bambu Studio CLI path (optional)</FieldLabel>
        <TextInput
          value={draft.bambu_studio_cli_path}
          onChange={(e) =>
            onChange({ ...draft, bambu_studio_cli_path: e.target.value })
          }
          placeholder="auto-detected from default install location"
          spellCheck={false}
        />
        <p className="mt-1 text-[11px] text-[var(--color-muted)]">
          Override only if Bambu Studio is installed in a non-standard
          location.
        </p>
      </div>
    </div>
  );
}
