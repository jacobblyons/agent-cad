import { useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

type Props = {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  width?: string; // tailwind width class, e.g. "w-[480px]"
  footer?: React.ReactNode;
};

export function Dialog({ open, title, onClose, children, width = "w-[480px]", footer }: Props) {
  // Esc closes.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={cn(
          "flex max-h-[80vh] flex-col overflow-hidden rounded-md border border-[var(--color-border-strong)] bg-[var(--color-panel)] shadow-2xl",
          width,
        )}
      >
        <div className="flex h-10 shrink-0 items-center justify-between border-b border-[var(--color-border)] bg-[var(--color-panel-2)] px-3">
          <span className="text-sm font-medium text-[var(--color-text)]">{title}</span>
          <button
            onClick={onClose}
            aria-label="close"
            className="rounded-sm p-1 text-[var(--color-muted)] hover:bg-[var(--color-hover)] hover:text-[var(--color-text)]"
          >
            <X size={14} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
        {footer && (
          <div className="flex h-12 shrink-0 items-center justify-end gap-2 border-t border-[var(--color-border)] bg-[var(--color-panel-2)] px-3">
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

export function PrimaryButton(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className, ...rest } = props;
  return (
    <button
      {...rest}
      className={cn(
        "rounded-sm bg-[var(--color-accent)] px-3 py-1 text-xs text-[var(--color-accent-fg)] hover:bg-[var(--color-accent-hover)] disabled:opacity-40",
        className,
      )}
    />
  );
}

export function SecondaryButton(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const { className, ...rest } = props;
  return (
    <button
      {...rest}
      className={cn(
        "rounded-sm border border-[var(--color-border)] px-3 py-1 text-xs text-[var(--color-text)] hover:bg-[var(--color-hover)] disabled:opacity-40",
        className,
      )}
    />
  );
}

export function FieldLabel({ children }: { children: React.ReactNode }) {
  return (
    <label className="mb-1 block text-[11px] uppercase tracking-wider text-[var(--color-muted)]">
      {children}
    </label>
  );
}

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  const { className, ...rest } = props;
  return (
    <input
      {...rest}
      className={cn(
        "w-full rounded-sm border border-[var(--color-border)] bg-[var(--color-bg)] px-2 py-1.5 text-sm text-[var(--color-text)] outline-none focus:border-[var(--color-focus)]",
        className,
      )}
    />
  );
}
