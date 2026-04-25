import { useMemo } from "react";
import { ChevronLeft, ChevronRight, GitCommit, Loader2 } from "lucide-react";
import { call } from "@/lib/pywebview";
import { useChat } from "@/lib/chat";
import { useDoc } from "@/lib/doc";
import { cn } from "@/lib/utils";

export function Timeline() {
  const { doc } = useDoc();
  const { isAgentRunning } = useChat();

  // Ordered oldest → newest so left arrow = back in time, right arrow = forward.
  const ordered = useMemo(
    () => (doc ? [...doc.commits].reverse() : []),
    [doc],
  );
  const currentIdx = useMemo(
    () => (doc ? ordered.findIndex((c) => c.sha === doc.head_sha) : -1),
    [doc, ordered],
  );
  const canBack = currentIdx > 0 && !isAgentRunning;
  const canForward = currentIdx >= 0 && currentIdx < ordered.length - 1 && !isAgentRunning;

  const onCheckout = async (sha: string) => {
    if (!doc || sha === doc.head_sha || isAgentRunning) return;
    await call("timeline_checkout", doc.id, sha);
  };

  const goBack = () => canBack && onCheckout(ordered[currentIdx - 1].sha);
  const goForward = () => canForward && onCheckout(ordered[currentIdx + 1].sha);

  if (!doc) {
    return (
      <div className="flex h-12 items-center px-3 text-xs text-[var(--color-muted)]">
        no project
      </div>
    );
  }

  return (
    <div className="flex h-12 items-center gap-2 overflow-x-auto px-3">
      <span className="shrink-0 text-xs uppercase tracking-wider text-[var(--color-muted)]">
        history
      </span>
      <div className="flex shrink-0 items-center gap-0.5 rounded-sm border border-[var(--color-border)]">
        <NavBtn
          onClick={goBack}
          disabled={!canBack}
          title="Step back in time (older commit)"
        >
          <ChevronLeft size={12} />
        </NavBtn>
        <NavBtn
          onClick={goForward}
          disabled={!canForward}
          title="Step forward in time (newer commit)"
        >
          <ChevronRight size={12} />
        </NavBtn>
      </div>
      {currentIdx >= 0 && (
        <span className="shrink-0 font-mono text-[10px] text-[var(--color-muted)]">
          {currentIdx + 1} / {ordered.length}
        </span>
      )}
      <div className="flex items-center gap-1.5 overflow-x-auto">
        {ordered.length === 0 && (
          <span className="text-xs text-[var(--color-muted)]">empty</span>
        )}
        {ordered.map((c) => {
          const active = c.sha === doc.head_sha;
          const disabled = isAgentRunning && !active;
          return (
            <button
              key={c.sha}
              disabled={disabled}
              title={
                isAgentRunning
                  ? "agent is working — wait to navigate"
                  : `${c.short} — ${c.subject}\n${c.author} · ${new Date(c.date).toLocaleString()}`
              }
              onClick={() => onCheckout(c.sha)}
              className={cn(
                "flex shrink-0 items-center gap-1.5 rounded-sm border px-2 py-1 text-xs",
                active
                  ? "border-[var(--color-focus)] bg-[var(--color-selection)] text-[var(--color-text)]"
                  : "border-[var(--color-border)] text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)] disabled:opacity-30 disabled:hover:bg-transparent",
              )}
            >
              <GitCommit size={10} />
              <span className="font-mono text-[10px] opacity-80">{c.short}</span>
              <span className="max-w-[160px] truncate">{c.subject}</span>
            </button>
          );
        })}
      </div>
      {isAgentRunning && (
        <span className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-sm bg-[var(--color-selection)] px-2 py-0.5 text-[10px] text-[var(--color-text)]">
          <Loader2 size={10} className="animate-spin" />
          locked while agent is working
        </span>
      )}
      {!isAgentRunning && doc.uncommitted && (
        <span className="ml-auto shrink-0 rounded-sm bg-[#7a4a1d]/40 px-2 py-0.5 text-[10px] text-[#dcb073]">
          uncommitted
        </span>
      )}
    </div>
  );
}

function NavBtn(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className, ...rest } = props;
  return (
    <button
      {...rest}
      className={cn(
        "flex h-6 w-6 items-center justify-center text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)] disabled:opacity-30 disabled:hover:bg-transparent",
        className,
      )}
    />
  );
}
