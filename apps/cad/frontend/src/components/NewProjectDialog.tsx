import { useEffect, useState } from "react";
import { call } from "@/lib/pywebview";
import type { DocSummary } from "@/lib/doc";
import { Dialog, FieldLabel, PrimaryButton, SecondaryButton, TextInput } from "./Dialog";

type Props = {
  open: boolean;
  onClose: () => void;
  onCreated: (doc: DocSummary) => void;
};

export function NewProjectDialog({ open, onClose, onCreated }: Props) {
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setName("");
      setError(null);
      setBusy(false);
    }
  }, [open]);

  const submit = async () => {
    const n = name.trim();
    if (!n) {
      setError("name is required");
      return;
    }
    setBusy(true);
    setError(null);
    const r = await call<{ ok: boolean; project?: DocSummary; error?: string }>(
      "project_create", n,
    );
    setBusy(false);
    if (!r.ok || !r.project) {
      setError(r.error || "could not create project");
      return;
    }
    onCreated(r.project);
    onClose();
  };

  return (
    <Dialog
      open={open}
      title="New project"
      onClose={onClose}
      footer={
        <>
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
          <PrimaryButton onClick={submit} disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create"}
          </PrimaryButton>
        </>
      }
    >
      <FieldLabel>Project name</FieldLabel>
      <TextInput
        autoFocus
        value={name}
        onChange={(e) => setName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="phone-stand"
      />
      <p className="mt-2 text-xs text-[var(--color-muted)]">
        A new folder will be created in your default project directory.
      </p>
      {error && (
        <div className="mt-3 rounded-sm border border-[#f48771] bg-[#3a1d1d] px-2 py-1.5 text-xs text-[#f48771]">
          {error}
        </div>
      )}
    </Dialog>
  );
}
