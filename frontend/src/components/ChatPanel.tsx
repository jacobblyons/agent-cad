import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Check,
  Circle,
  CircleHelp,
  Globe,
  ListChecks,
  Loader2,
  Pencil,
  Send,
  ShieldCheck,
  ShieldX,
  Wrench,
  X,
} from "lucide-react";
import { DrawingDialog } from "./DrawingDialog";
import { Markdown } from "./Markdown";
import { call } from "@/lib/pywebview";
import {
  useChat,
  type AgentTodo,
  type ChatBlock,
  type ChatPermissionBlock,
  type ChatToolBlock,
  type Turn,
} from "@/lib/chat";
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

/** Renders text capped to `collapsedMax` px tall with a bottom fade and an
 * Expand toggle when the content overflows. The whole chat scroller is
 * already scrollable, so giving each tool result its own inner scrollbar
 * was the worst of both worlds — pick one or the other. */
function CollapsibleText({
  text,
  collapsedMax = 160,
  fadeVar = "--color-panel-2",
  className,
}: {
  text: string;
  collapsedMax?: number;
  /** CSS variable name for the parent bg, used for the fade-out gradient. */
  fadeVar?: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflows, setOverflows] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      // Compare unconstrained content height to the cap. We measure
      // against the inner content wrapper, not the outer (which has
      // maxHeight applied), so scrollHeight is the FULL height.
      setOverflows(el.scrollHeight > collapsedMax + 1);
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [text, collapsedMax]);

  return (
    <div className={cn("relative", className)}>
      <div
        ref={ref}
        style={{
          maxHeight: expanded ? undefined : collapsedMax,
          overflow: "hidden",
        }}
        className="whitespace-pre-wrap break-all"
      >
        {text}
      </div>
      {overflows && !expanded && (
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 bottom-6 h-6"
          style={{
            background: `linear-gradient(to top, var(${fadeVar}), transparent)`,
          }}
        />
      )}
      {overflows && (
        <button
          onClick={() => setExpanded((e) => !e)}
          className="mt-1 text-[10px] uppercase tracking-wider text-[var(--color-muted)] hover:text-[var(--color-text)]"
        >
          {expanded ? "Collapse" : "Expand"}
        </button>
      )}
    </div>
  );
}

export function ChatPanel() {
  const { doc } = useDoc();
  const {
    turns,
    isAgentRunning,
    send,
    pendingAttachments,
    addAttachment,
    removeAttachment,
    todos,
  } = useChat();
  const [input, setInput] = useState("");
  const [showDraw, setShowDraw] = useState(false);
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

  const canSubmit =
    !!doc && !isAgentRunning && (input.trim().length > 0 || pendingAttachments.length > 0);

  const submit = async () => {
    if (!canSubmit) return;
    const text = input.trim();
    setInput("");
    // App.send drains pendingAttachments from the active tab.
    await send(text);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <TasksPanel todos={todos} />
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
              className="ml-auto max-w-[90%] space-y-1.5 rounded-md bg-[var(--color-selection)] px-3 py-2 text-sm leading-relaxed text-[var(--color-text)]"
            >
              {t.text && <div>{t.text}</div>}
              {t.images && t.images.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {t.images.map((img, i) => (
                    <img
                      key={i}
                      src={`data:${img.mimeType};base64,${img.data}`}
                      alt="sketch"
                      className="max-h-40 rounded-sm border border-[var(--color-border)] bg-white object-contain"
                    />
                  ))}
                </div>
              )}
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
        <div className="flex flex-col gap-2 rounded-md border border-[var(--color-border)] bg-[var(--color-panel-2)] px-3 py-2 focus-within:border-[var(--color-focus)]">
          {pendingAttachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {pendingAttachments.map((img, i) => (
                <div key={i} className="relative">
                  <img
                    src={`data:${img.mimeType};base64,${img.data}`}
                    alt="attached sketch"
                    className="h-14 w-14 rounded-sm border border-[var(--color-border)] bg-white object-contain"
                  />
                  <button
                    onClick={() => removeAttachment(i)}
                    aria-label="remove attachment"
                    className="absolute -right-1.5 -top-1.5 flex h-4 w-4 items-center justify-center rounded-full border border-[var(--color-border)] bg-[var(--color-panel)] text-[var(--color-muted)] hover:text-[var(--color-text)]"
                  >
                    <X size={10} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
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
              placeholder="Describe a part, sketch a hint, or paste an image…"
              rows={1}
              className="min-h-[24px] flex-1 resize-none overflow-y-auto bg-transparent text-sm leading-relaxed outline-none placeholder:text-[var(--color-muted)]"
            />
            <button
              onClick={() => setShowDraw(true)}
              disabled={!doc}
              title="Add a sketch"
              aria-label="add sketch"
              className="flex h-8 w-8 items-center justify-center rounded-sm border border-[var(--color-border)] text-[var(--color-text)] hover:bg-[var(--color-hover)] disabled:opacity-40"
            >
              <Pencil size={14} />
            </button>
            <button
              onClick={submit}
              disabled={!canSubmit}
              className="flex h-8 w-8 items-center justify-center rounded-sm bg-[var(--color-accent)] text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
              aria-label="send"
            >
              <Send size={14} />
            </button>
          </div>
        </div>
      </div>
      <DrawingDialog
        open={showDraw}
        onClose={() => setShowDraw(false)}
        onAttach={(img) => addAttachment({ ...img, source: "drawing" })}
      />
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
      {turn.blocks.map((b: ChatBlock, i: number) => {
        if (b.kind === "text") {
          return (
            <div
              key={i}
              className="max-w-[95%] rounded-md bg-[var(--color-panel-2)] px-3 py-2"
            >
              <Markdown text={b.text} />
            </div>
          );
        }
        if (b.kind === "permission") {
          return <PermissionCard key={i} block={b} />;
        }
        return <ToolCard key={i} block={b} />;
      })}
      {turn.errorText && (
        <div className="max-w-[95%] rounded-md border border-[#f48771] bg-[#3a1d1d] px-3 py-2 text-xs text-[#f48771]">
          {turn.errorText}
        </div>
      )}
    </div>
  );
}

function PermissionCard({ block }: { block: ChatPermissionBlock }) {
  const [busy, setBusy] = useState(false);

  const respond = async (approved: boolean) => {
    if (busy || block.status !== "pending") return;
    setBusy(true);
    try {
      await call("permission_resolve", block.requestId, approved, "");
    } finally {
      // We don't optimistically flip state — the backend's
      // permission_resolved event closes the loop and updates the block.
      setBusy(false);
    }
  };

  const isPlaywright = block.tool.startsWith("mcp__playwright__");
  const Icon = isPlaywright ? Globe : ShieldCheck;
  const friendlyTool = block.tool
    .replace(/^mcp__playwright__/, "playwright.")
    .replace(/^mcp__([^_]+)__/, "$1.")
    .replace(/^mcp__/, "");

  return (
    <div
      className={cn(
        "w-full max-w-[95%] overflow-hidden rounded-md border px-3 py-2.5 text-sm",
        block.status === "pending"
          ? "border-[var(--color-focus)] bg-[var(--color-panel-2)]"
          : block.status === "approved"
            ? "border-[var(--color-border)] bg-[var(--color-panel-2)] text-[var(--color-muted)]"
            : "border-[#f48771]/60 bg-[var(--color-panel-2)] text-[var(--color-muted)]",
      )}
    >
      <div className="flex items-start gap-2">
        <Icon
          size={14}
          className={cn(
            "mt-0.5 shrink-0",
            block.status === "pending"
              ? "text-[var(--color-focus)]"
              : block.status === "approved"
                ? "text-[var(--color-accent)]"
                : "text-[#f48771]",
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
            {block.status === "pending"
              ? "Permission requested"
              : block.status === "approved"
                ? "Approved"
                : block.status === "timeout"
                  ? "Timed out"
                  : "Denied"}
          </div>
          <div className="font-mono text-xs text-[var(--color-text)]">
            {friendlyTool}({fmtToolInput(block.input)})
          </div>
          {block.status === "pending" && (
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={() => respond(true)}
                disabled={busy}
                className="flex h-7 items-center gap-1 rounded-sm bg-[var(--color-accent)] px-2 text-xs text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40"
              >
                <ShieldCheck size={11} />
                <span>Approve</span>
              </button>
              <button
                onClick={() => respond(false)}
                disabled={busy}
                className="flex h-7 items-center gap-1 rounded-sm border border-[var(--color-border)] px-2 text-xs text-[var(--color-text)] hover:bg-[var(--color-hover)] disabled:opacity-40"
              >
                <ShieldX size={11} />
                <span>Deny</span>
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ToolCard({ block }: { block: ChatToolBlock }) {
  // AskUserQuestion is rendered specially: the agent is pausing to ask
  // the user something, so we want it to look like a question prompt
  // rather than a routine tool call.
  if (block.tool === "AskUserQuestion") {
    return <AskUserQuestionCard block={block} />;
  }
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
        <CollapsibleText
          text={block.resultText}
          className="mt-1 pl-5 font-mono"
        />
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

function AskUserQuestionCard({ block }: { block: ChatToolBlock }) {
  // The exact input shape varies between SDK versions; pull common fields
  // best-effort. Typically: { question: string, options?: string[] } or
  // { questions: [{ question, options }] }.
  const input = (block.input ?? {}) as Record<string, unknown>;
  const single = typeof input.question === "string" ? input.question : null;
  const list = Array.isArray(input.questions)
    ? (input.questions as Array<Record<string, unknown>>)
    : null;
  const options = Array.isArray(input.options)
    ? (input.options as unknown[]).map((o) => String(o))
    : null;
  return (
    <div className="w-full max-w-[95%] overflow-hidden rounded-md border border-[var(--color-focus)] bg-[var(--color-panel-2)] px-3 py-2.5 text-sm">
      <div className="flex items-start gap-2 text-[var(--color-text)]">
        <CircleHelp
          size={14}
          className="mt-0.5 shrink-0 text-[var(--color-focus)]"
        />
        <div className="min-w-0 flex-1 space-y-1.5">
          {single && <div className="leading-relaxed">{single}</div>}
          {list?.map((q, i) => {
            const text = typeof q.question === "string" ? q.question : "";
            const qOptions = Array.isArray(q.options)
              ? (q.options as unknown[]).map((o) => String(o))
              : null;
            return (
              <div key={i}>
                <div className="leading-relaxed">{text}</div>
                {qOptions && qOptions.length > 0 && (
                  <ul className="mt-1 list-disc pl-5 text-xs text-[var(--color-muted)]">
                    {qOptions.map((opt, j) => (
                      <li key={j}>{opt}</li>
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
          {options && options.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-[var(--color-muted)]">
              {options.map((opt, i) => (
                <li key={i}>{opt}</li>
              ))}
            </ul>
          )}
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-muted)]">
            type your reply below
          </div>
        </div>
      </div>
      {block.resultText && (
        <CollapsibleText
          text={block.resultText}
          className="mt-2 pl-6 text-xs text-[var(--color-muted)]"
        />
      )}
    </div>
  );
}

function TasksPanel({ todos }: { todos: AgentTodo[] }) {
  // Only render when there's at least one item that isn't fully done — once
  // every todo is "completed" the panel collapses so it doesn't take up
  // space across follow-up turns.
  const hasOpen = useMemo(
    () => todos.some((t) => t.status !== "completed"),
    [todos],
  );
  if (todos.length === 0 || !hasOpen) return null;

  const total = todos.length;
  const done = todos.filter((t) => t.status === "completed").length;

  return (
    <div className="shrink-0 border-b border-[var(--color-border)] bg-[var(--color-panel-2)] px-3 py-2 text-xs">
      <div className="mb-1 flex items-center gap-1.5 text-[var(--color-muted)]">
        <ListChecks size={12} />
        <span className="text-[10px] uppercase tracking-wider">tasks</span>
        <span className="ml-auto tabular-nums">
          {done} / {total}
        </span>
      </div>
      <ul className="space-y-1">
        {todos.map((todo, i) => (
          <li key={i} className="flex items-start gap-2 leading-snug">
            <TodoIcon status={todo.status} />
            <span
              className={cn(
                "min-w-0 flex-1 break-words",
                todo.status === "completed" &&
                  "text-[var(--color-muted)] line-through",
                todo.status === "in_progress" && "text-[var(--color-text)]",
                todo.status === "pending" && "text-[var(--color-muted)]",
              )}
            >
              {todo.status === "in_progress" && todo.activeForm
                ? todo.activeForm
                : todo.content}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TodoIcon({ status }: { status: AgentTodo["status"] }) {
  if (status === "completed") {
    return (
      <Check
        size={11}
        className="mt-0.5 shrink-0 text-[var(--color-accent)]"
      />
    );
  }
  if (status === "in_progress") {
    return (
      <Loader2
        size={11}
        className="mt-0.5 shrink-0 animate-spin text-[var(--color-focus)]"
      />
    );
  }
  return (
    <Circle size={11} className="mt-0.5 shrink-0 text-[var(--color-muted)]" />
  );
}
