/**
 * Thin wrapper around window.pywebview.api.
 *
 * pywebview injects window.pywebview asynchronously after the page loads —
 * `pywebviewready` fires when it's available. In Vite dev mode (browser, no
 * pywebview), all calls fall back to a stub so the UI keeps working.
 */

type ApiMethod = (...args: unknown[]) => Promise<unknown>;

declare global {
  interface Window {
    pywebview?: {
      api: Record<string, ApiMethod>;
    };
  }
}

const ready: { resolve: () => void; promise: Promise<void> } = (() => {
  let resolveFn: () => void = () => {};
  const promise = new Promise<void>((resolve) => {
    resolveFn = resolve;
  });
  return { resolve: resolveFn, promise };
})();

if (typeof window !== "undefined") {
  if (window.pywebview) {
    ready.resolve();
  } else {
    window.addEventListener("pywebviewready", () => ready.resolve(), { once: true });
    // In a plain browser (no pywebview), unblock after a short delay so the UI is usable.
    setTimeout(() => ready.resolve(), 250);
  }
}

export async function call<T = unknown>(method: string, ...args: unknown[]): Promise<T> {
  await ready.promise;
  const api = window.pywebview?.api;
  if (!api || typeof api[method] !== "function") {
    console.warn(`[pywebview stub] ${method}`, args);
    return { ok: false, stub: true } as T;
  }
  return api[method](...args) as Promise<T>;
}

export function on<T = unknown>(channel: string, handler: (payload: T) => void): () => void {
  const wrapped = (e: Event) => handler((e as CustomEvent<T>).detail);
  window.addEventListener(`agentcad:${channel}`, wrapped as EventListener);
  return () => window.removeEventListener(`agentcad:${channel}`, wrapped as EventListener);
}
