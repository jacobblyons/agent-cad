import { FolderOpen, Plus, Settings } from "lucide-react";

type Props = {
  onNew: () => void;
  onOpen: () => void;
  onSettings: () => void;
};

export function Welcome({ onNew, onOpen, onSettings }: Props) {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-[var(--color-bg)]">
      <div className="flex w-[420px] flex-col gap-3 rounded-md border border-[var(--color-border-strong)] bg-[var(--color-panel)] p-6">
        <div className="mb-2">
          <div className="text-base font-medium text-[var(--color-text)]">Agent CAD</div>
          <div className="text-xs text-[var(--color-muted)]">
            LLM-driven parametric CAD
          </div>
        </div>
        <WelcomeButton icon={<Plus size={14} />} label="New project" hint="Ctrl+N" onClick={onNew} />
        <WelcomeButton icon={<FolderOpen size={14} />} label="Open project" hint="Ctrl+O" onClick={onOpen} />
        <WelcomeButton icon={<Settings size={14} />} label="Settings" hint="Ctrl+," onClick={onSettings} />
      </div>
    </div>
  );
}

function WelcomeButton({
  icon, label, hint, onClick,
}: { icon: React.ReactNode; label: string; hint: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="flex items-center justify-between rounded-sm border border-[var(--color-border)] bg-[var(--color-panel-2)] px-3 py-2 text-sm text-[var(--color-text)] hover:bg-[var(--color-hover)]"
    >
      <span className="inline-flex items-center gap-2">
        {icon}
        {label}
      </span>
      <span className="font-mono text-[10px] text-[var(--color-muted)]">{hint}</span>
    </button>
  );
}
