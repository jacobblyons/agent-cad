import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { cn } from "@/lib/utils";

// Components map. Each override styles a markdown node to fit the chat
// panel theme — small body text, monospaced code, indented lists, etc.
// Keys are the standard react-markdown element names; we don't use HTML
// tag overrides for things like h4/h5 (rare in agent replies).
const COMPONENTS: Components = {
  p: ({ children }) => <p className="my-1 first:mt-0 last:mb-0">{children}</p>,
  strong: ({ children }) => (
    <strong className="font-semibold text-[var(--color-text)]">{children}</strong>
  ),
  em: ({ children }) => <em className="italic">{children}</em>,
  code: ({ className, children, ...rest }) => {
    // react-markdown calls this for both inline and fenced code; only
    // fenced has a language class. The parent <pre> wraps fenced ones.
    const isBlock = /\blanguage-/.test(className ?? "");
    if (isBlock) {
      return (
        <code className={cn(className, "font-mono text-[12px] leading-snug")} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code
        className="rounded-sm bg-[var(--color-panel)] px-1 py-px font-mono text-[12px] text-[var(--color-text)]"
        {...rest}
      >
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    <pre className="my-1.5 overflow-x-auto rounded-sm border border-[var(--color-border)] bg-[var(--color-panel)] p-2">
      {children}
    </pre>
  ),
  ul: ({ children }) => (
    <ul className="my-1 list-disc space-y-0.5 pl-5 marker:text-[var(--color-muted)]">{children}</ul>
  ),
  ol: ({ children }) => (
    <ol className="my-1 list-decimal space-y-0.5 pl-5 marker:text-[var(--color-muted)]">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="pl-0.5">{children}</li>,
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-[var(--color-accent)] underline decoration-dotted underline-offset-2 hover:opacity-80"
    >
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-1 border-l-2 border-[var(--color-border)] pl-3 text-[var(--color-muted)]">
      {children}
    </blockquote>
  ),
  h1: ({ children }) => <div className="my-1 text-base font-semibold">{children}</div>,
  h2: ({ children }) => <div className="my-1 text-sm font-semibold">{children}</div>,
  h3: ({ children }) => <div className="my-1 text-sm font-semibold">{children}</div>,
  hr: () => <hr className="my-2 border-[var(--color-border)]" />,
  table: ({ children }) => (
    <div className="my-1.5 overflow-x-auto">
      <table className="border-collapse text-xs">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border border-[var(--color-border)] px-2 py-0.5 text-left font-semibold">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-[var(--color-border)] px-2 py-0.5">{children}</td>
  ),
};

/** Renders agent text as GitHub-flavored markdown using the chat theme.
 * Keep the wrapper `div` on the same `text-sm leading-relaxed` baseline
 * the surrounding bubble uses — paragraph margins inside collapse to it. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={cn("text-sm leading-relaxed", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
