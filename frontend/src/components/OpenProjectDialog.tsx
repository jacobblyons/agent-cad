import { useEffect, useState } from "react";
import { FolderOpen, GitCommit } from "lucide-react";
import { call } from "@/lib/pywebview";
import type { DocSummary } from "@/lib/doc";
import { Dialog, SecondaryButton } from "./Dialog";

type RecentProject = {
  path: string;
  title: string;
  head_sha: string;
  head_subject: string;
  modified: number;
};

type ListResponse = {
  ok: boolean;
  default_dir: string;
  projects: RecentProject[];
};

type OpenResponse = { ok: boolean; project?: DocSummary; error?: string; cancelled?: boolean };

type Props = {
  open: boolean;
  onClose: () => void;
  onOpened: (doc: DocSummary) => void;
};

export function OpenProjectDialog({ open, onClose, onOpened }: Props) {
  const [list, setList] = useState<ListResponse | null>(null);
  const [busyPath, setBusyPath] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    (async () => {
      const r = await call<ListResponse>("project_list_recent");
      setList(r);
    })();
  }, [open]);

  const openPath = async (path: string) => {
    setBusyPath(path);
    setError(null);
    const r = await call<OpenResponse>("project_open", path);
    setBusyPath(null);
    if (!r.ok || !r.project) {
      setError(r.error || "could not open project");
      return;
    }
    onOpened(r.project);
    onClose();
  };

  const openExternal = async () => {
    setBusyPath("external");
    setError(null);
    const r = await call<OpenResponse>("project_pick_external");
    setBusyPath(null);
    if (r.cancelled) return;
    if (!r.ok || !r.project) {
      setError(r.error || "could not open project");
      return;
    }
    onOpened(r.project);
    onClose();
  };

  return (
    <Dialog
      open={open}
      title="Open project"
      onClose={onClose}
      width="w-[560px]"
      footer={
        <>
          <SecondaryButton onClick={openExternal} disabled={busyPath !== null}>
            <span className="inline-flex items-center gap-1.5">
              <FolderOpen size={12} />
              {busyPath === "external" ? "Opening…" : "Open external folder…"}
            </span>
          </SecondaryButton>
          <SecondaryButton onClick={onClose}>Cancel</SecondaryButton>
        </>
      }
    >
      <div className="mb-2 text-xs text-[var(--color-muted)]">
        {list ? (
          <>
            Recent projects in{" "}
            <span className="font-mono text-[var(--color-text)]">{list.default_dir}</span>
          </>
        ) : (
          "loading…"
        )}
      </div>
      <div className="space-y-1">
        {list?.projects.length === 0 && (
          <div className="rounded-sm border border-dashed border-[var(--color-border)] p-3 text-xs text-[var(--color-muted)]">
            no projects yet — create one or open an external folder
          </div>
        )}
        {list?.projects.map((p) => (
          <button
            key={p.path}
            onClick={() => openPath(p.path)}
            disabled={busyPath !== null}
            className="flex w-full flex-col items-start gap-0.5 rounded-sm border border-transparent px-2 py-1.5 text-left hover:border-[var(--color-border)] hover:bg-[var(--color-hover)] disabled:opacity-40"
          >
            <span className="text-sm text-[var(--color-text)]">{p.title}</span>
            <span className="flex items-center gap-1 font-mono text-[10px] text-[var(--color-muted)]">
              {p.head_sha && (
                <>
                  <GitCommit size={10} />
                  <span>{p.head_sha.slice(0, 7)}</span>
                </>
              )}
              {p.head_subject && <span className="truncate">— {p.head_subject}</span>}
            </span>
          </button>
        ))}
      </div>
      {error && (
        <div className="mt-3 rounded-sm border border-[#f48771] bg-[#3a1d1d] px-2 py-1.5 text-xs text-[#f48771]">
          {error}
        </div>
      )}
    </Dialog>
  );
}
