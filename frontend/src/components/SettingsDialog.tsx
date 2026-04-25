import { useEffect, useState } from "react";
import { FolderOpen } from "lucide-react";
import { call } from "@/lib/pywebview";
import { Dialog, FieldLabel, PrimaryButton, SecondaryButton, TextInput } from "./Dialog";

type Settings = { model: string; default_project_dir: string; effort: string };
type ModelOpt = { id: string; label: string; tier: string };
type EffortOpt = { id: string; label: string };
type GetResponse = {
  ok: boolean;
  settings: Settings;
  models: ModelOpt[];
  efforts: EffortOpt[];
};

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
