import { useEffect, useRef, useState } from "react";
import { Loader2, Send, Wrench } from "lucide-react";
import { useChat, type ChatBlock, type ChatToolBlock, type Turn } from "@/lib/chat";
import { useDoc } from "@/lib/doc";
import { cn } from "@/lib/utils";

function fmtToolName(name: string) {
  return name.replace(/^mcp__cad__/, "");
}

function fmtToolInput(input: unknown) {
  if (input == null || typeof input !== "object") return "";
  return Object.entries(input as Record<string, unknown>)
    .map(([k, v]) => {
      let s = typeof v === "string" ? `"${v}"` : JSON.stringify(v);
      if (s && s.length > 80) s = s.slice(0, 77) + "…";
      return `${k}=${s}`;
    })
    .join(", ");
}

const TEXTAREA_MAX_PX = 240;

export function ChatPanel() {
  const { doc } = useDoc();
  const { turns, isAgentRunning, send } = useChat();
  const [input, setInput] = useState("");
  const scrollerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    scrollerRef.current?.scrollTo({
      top: scrollerRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns]);

  // Auto-resize textarea to its content, capped at TEXTAREA_MAX_PX.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, TEXTAREA_MAX_PX)}px`;
  }, [input]);

  const submit = async () => {
    const text = input.trim();
    if (!text || !doc || isAgentRunning) return;
    setInput("");
    await send(text);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scrollerRef} className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {turns.length === 0 && (
          <div className="rounded-md bg-[var(--color-panel-2)] px-3 py-2 text-sm text-[var(--color-muted)]">
            Describe a part and I&apos;ll model it. Click on a face in the viewer to
            point at something specific.
          </div>
        )}
        {turns.map((t) =>
          t.role === "user" ? (
            <div
              key={t.id}
              className="ml-auto max-w-[90%] rounded-md bg-[var(--color-selection)] px-3 py-2 text-sm leading-relaxed text-[var(--color-text)]"
            >
              {t.text}
            </div>
          ) : (
            <AssistantTurn key={t.id} turn={t} />
          ),
        )}
        {isAgentRunning && (
          <div className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
            <Loader2 size={12} className="animate-spin" />
            <span>thinking…</span>
          </div>
        )}
      </div>
      <div className="border-t border-[var(--color-border)] p-3">
        <div className="flex items-end gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)] px-3 py-2 focus-within:border-[var(--color-focus)]">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder="Describe a part, or paste an image…"
            rows={1}
            className="min-h-[24px] flex-1 resize-none overflow-y-auto bg-transparent text-sm leading-relaxed outline-none placeholder:text-[var(--color-muted)]"
          />
          <button
            onClick={submit}
            disabled={!input.trim() || !doc || isAgentRunning}
            className="flex h-8 w-8 items-center justify-center rounded-sm bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
            aria-label="send"
          >
            <Send size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

function AssistantTurn({
  turn,
}: {
  turn: Extract<Turn, { role: "assistant" }>;
}) {
  return (
    <div className="space-y-2">
      {turn.blocks.map((b: ChatBlock, i: number) =>
        b.kind === "text" ? (
          <div
            key={i}
            className="max-w-[95%] whitespace-pre-wrap rounded-md bg-[var(--color-panel-2)] px-3 py-2 text-sm leading-relaxed"
          >
            {b.text}
          </div>
        ) : (
          <ToolCard key={i} block={b} />
        ),
      )}
      {turn.errorText && (
        <div className="max-w-[95%] rounded-md border border-[#f48771] bg-[#3a1d1d] px-3 py-2 text-xs text-[#f48771]">
          {turn.errorText}
        </div>
      )}
    </div>
  );
}

function ToolCard({ block }: { block: ChatToolBlock }) {
  return (
    <div
      className={cn(
        "w-full overflow-hidden rounded-md border px-3 py-2 text-xs",
        block.isError
          ? "border-[#f48771] bg-[#3a1d1d] text-[#f48771]"
          : "border-[var(--color-border)] bg-[var(--color-panel-2)] text-[var(--color-muted)]",
      )}
    >
      <div className="flex items-start gap-2 text-[var(--color-text)]">
        <Wrench size={12} className="mt-0.5 shrink-0" />
        <span className="min-w-0 flex-1 break-all font-mono">
          {fmtToolName(block.tool)}({fmtToolInput(block.input)})
        </span>
      </div>
      {block.resultText && (
        <div className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-all pl-5 font-mono">
          {block.resultText}
        </div>
      )}
      {block.resultImages && block.resultImages.length > 0 && (
        <div className="mt-2 space-y-1.5 pl-5">
          {block.resultImages.map((img, i) => (
            <img
              key={i}
              src={`data:${img.mimeType};base64,${img.data}`}
              alt="snapshot"
              className="block max-h-72 w-full rounded-sm border border-[var(--color-border)] object-contain bg-[var(--color-bg)]"
            />
          ))}
        </div>
      )}
    </div>
  );
}
