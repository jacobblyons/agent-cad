import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { NewProjectDialog } from "@/components/NewProjectDialog";
import { OpenProjectDialog } from "@/components/OpenProjectDialog";
import { SettingsDialog } from "@/components/SettingsDialog";
import { Welcome } from "@/components/Welcome";
import {
  BrowserContext,
  type BrowserCtx,
  type BrowserFrame,
  type BrowserState,
} from "@/lib/browser";
import {
  ChatContext,
  formatAttachmentsForPrompt,
  formatPinForPrompt,
  formatPinForUser,
  type ChatBlock,
  type ChatPermissionBlock,
  type ChatImage,
  type ChatToolBlock,
  type PinInfo,
  type Turn,
} from "@/lib/chat";
import { DocContext, type DocSummary } from "@/lib/doc";
import { call, on } from "@/lib/pywebview";
import { TabsContext, type TabState } from "@/lib/tabs";
import { UiContext } from "@/lib/ui";
import { ViewerContext } from "@/lib/viewer";

type StateEvent = { doc_id: string; state: DocSummary };

type GeometryEvent = {
  doc_id: string;
  object: string;
  glb_b64?: string;
  topology?: import("@/lib/viewer").Topology | null;
  error?: string;
  loading?: boolean;
};

type SketchGeometryEvent = {
  doc_id: string;
  sketch: string;
  ok?: boolean;
  error?: string;
  deleted?: boolean;
  polylines?: { points: [number, number, number][]; closed: boolean }[] | null;
  dimensions?: import("@/lib/viewer").SketchDimension[] | null;
  plane?: import("@/lib/viewer").SketchGeometry["plane"];
  loading?: boolean;
};

type ImportGeometryEvent = {
  doc_id: string;
  import: string;
  ok?: boolean;
  error?: string;
  deleted?: boolean;
  glb_b64?: string;
  topology?: import("@/lib/viewer").Topology | null;
  loading?: boolean;
};

type PermissionRequestEvent = {
  doc_id: string;
  msg_id: string;
  request_id: string;
  tool: string;
  input: unknown;
  tool_use_id?: string;
};

type PermissionResolvedEvent = {
  doc_id: string;
  msg_id: string;
  request_id: string;
  approved: boolean;
  message?: string;
};

type PlaywrightFrameEvent =
  | {
      kind: "session_started";
      session_id?: string;
      url?: string;
      title?: string;
    }
  | {
      kind: "session_ended";
      session_id?: string;
    }
  | {
      kind: "navigated";
      session_id?: string;
      url?: string;
    }
  | {
      kind: "frame";
      session_id?: string;
      data: string;
      mime: string;
      device_width?: number;
      device_height?: number;
      page_scale_factor?: number;
    };

type ChatEvent = {
  doc_id: string;
  msg_id: string;
  kind: "start" | "text" | "tool_use" | "tool_result" | "result" | "error" | "done";
  text?: string;
  tool?: string;
  input?: unknown;
  tool_use_id?: string;
  is_error?: boolean;
  images?: import("@/lib/chat").ChatImage[];
};

export default function App() {
  const [tabs, setTabs] = useState<TabState[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [showNew, setShowNew] = useState(false);
  const [showOpen, setShowOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [browserState, setBrowserState] = useState<BrowserState>({
    active: false,
    url: null,
    title: null,
    frame: null,
    lastFrameAt: null,
  });
  const [browserCollapsed, setBrowserCollapsed] = useState(true);

  const activeTab = useMemo(
    () => tabs.find((t) => t.doc.id === activeId) ?? null,
    [tabs, activeId],
  );
  const doc = activeTab?.doc ?? null;

  // --- tab actions -----------------------------------------------------

  const addTab = useCallback((d: DocSummary) => {
    setTabs((cur) => {
      // De-dupe: opening the same project twice just focuses the existing tab.
      const existing = cur.find((t) => t.doc.id === d.id);
      if (existing) return cur.map((t) => (t.doc.id === d.id ? { ...t, doc: d } : t));
      return [
        ...cur,
        {
          doc: d,
          turns: [],
          geometry: {},
          sketchGeometry: {},
          importGeometry: {},
          pendingAttachments: [],
          todos: [],
        },
      ];
    });
    setActiveId(d.id);
  }, []);

  const focusTab = useCallback((id: string) => setActiveId(id), []);

  const closeTab = useCallback(
    (id: string) => {
      setTabs((cur) => {
        const target = cur.find((t) => t.doc.id === id);
        if (!target) return cur;
        // Refuse close while agent is running.
        const running = target.turns.some(
          (t) => t.role === "assistant" && t.status === "running",
        );
        if (running) {
          window.alert("agent is still working — wait for it to finish before closing this tab");
          return cur;
        }
        if (target.doc.uncommitted) {
          const ok = window.confirm(
            `"${target.doc.title}" has uncommitted changes. Close anyway?`,
          );
          if (!ok) return cur;
        }
        const next = cur.filter((t) => t.doc.id !== id);
        // Pick a neighbor for active.
        if (activeId === id) {
          const i = cur.findIndex((t) => t.doc.id === id);
          const neighbor = next[i] ?? next[i - 1] ?? null;
          setActiveId(neighbor?.doc.id ?? null);
        }
        // Tell backend to release.
        call("project_close", id);
        return next;
      });
    },
    [activeId],
  );

  const cycleTab = useCallback(
    (delta: 1 | -1) => {
      if (tabs.length === 0) return;
      const i = Math.max(0, tabs.findIndex((t) => t.doc.id === activeId));
      const next = (i + delta + tabs.length) % tabs.length;
      setActiveId(tabs[next].doc.id);
    },
    [tabs, activeId],
  );

  const jumpTab = useCallback(
    (idx: number) => {
      const t = tabs[idx];
      if (t) setActiveId(t.doc.id);
    },
    [tabs],
  );

  // --- doc-level helpers (active tab) ---------------------------------

  const refresh = useCallback(async () => {
    if (!doc) return;
    await call("project_refresh", doc.id);
  }, [doc]);

  const save = useCallback(async () => {
    if (!doc) return;
    await call("project_commit", doc.id, "save");
  }, [doc]);

  const send = useCallback(
    async (text: string, opts?: { pin?: PinInfo; images?: ChatImage[] }) => {
      if (!doc) return;
      const pin = opts?.pin;
      const tab = tabs.find((t) => t.doc.id === doc.id);
      // Caller-supplied images take precedence; otherwise drain the per-tab
      // queue of pending attachments.
      const images = opts?.images ?? tab?.pendingAttachments ?? [];
      if (!text.trim() && images.length === 0) return;
      const display = pin ? formatPinForUser(pin, text) : text;
      let prompt = pin ? formatPinForPrompt(pin, text) : text;
      if (images.length > 0) prompt = formatAttachmentsForPrompt(images, prompt);
      const turn: Turn = {
        id: `u_${Date.now()}`,
        role: "user",
        text: display,
        images: images.length > 0 ? images : undefined,
      };
      setTabs((cur) =>
        cur.map((t) =>
          t.doc.id === doc.id
            ? { ...t, turns: [...t.turns, turn], pendingAttachments: [] }
            : t,
        ),
      );
      await call("chat_send", doc.id, prompt, images);
    },
    [doc, tabs],
  );

  const addAttachment = useCallback(
    (img: ChatImage) => {
      if (!doc) return;
      setTabs((cur) =>
        cur.map((t) =>
          t.doc.id === doc.id
            ? { ...t, pendingAttachments: [...t.pendingAttachments, img] }
            : t,
        ),
      );
    },
    [doc],
  );

  const removeAttachment = useCallback(
    (index: number) => {
      if (!doc) return;
      setTabs((cur) =>
        cur.map((t) =>
          t.doc.id === doc.id
            ? {
                ...t,
                pendingAttachments: t.pendingAttachments.filter((_, i) => i !== index),
              }
            : t,
        ),
      );
    },
    [doc],
  );

  // --- backend → per-tab event routing --------------------------------

  useEffect(() => {
    return on<StateEvent>("project_state", (p) => {
      setTabs((cur) =>
        cur.map((t) => (t.doc.id === p.doc_id ? { ...t, doc: p.state } : t)),
      );
    });
  }, []);

  useEffect(() => {
    return on<GeometryEvent>("doc_geometry", (p) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== p.doc_id) return t;
          const prev = t.geometry[p.object] ?? {
            glbB64: null,
            topology: null,
            errorMsg: null,
          };
          // A "loading" event keeps the previous geometry visible (so the
          // viewer doesn't blank out) but flips the spinner on. The
          // resolved event clears loading and updates the GLB / error.
          const loading = p.loading === true;
          return {
            ...t,
            geometry: {
              ...t.geometry,
              [p.object]: loading
                ? { ...prev, loading: true }
                : {
                    glbB64: p.glb_b64 ?? prev.glbB64,
                    topology: p.topology ?? prev.topology,
                    errorMsg: p.error ?? null,
                    loading: false,
                  },
            },
          };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<SketchGeometryEvent>("doc_sketch_geometry", (p) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== p.doc_id) return t;
          if (p.deleted) {
            const next = { ...t.sketchGeometry };
            delete next[p.sketch];
            return { ...t, sketchGeometry: next };
          }
          const prev = t.sketchGeometry[p.sketch] ?? {
            polylines: null,
            dimensions: null,
            plane: null,
            errorMsg: null,
          };
          const loading = p.loading === true;
          return {
            ...t,
            sketchGeometry: {
              ...t.sketchGeometry,
              [p.sketch]: loading
                ? { ...prev, loading: true }
                : {
                    polylines: p.polylines ?? prev.polylines,
                    dimensions: p.dimensions ?? prev.dimensions,
                    plane: p.plane ?? prev.plane,
                    errorMsg: p.error ?? null,
                    loading: false,
                  },
            },
          };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<ImportGeometryEvent>("doc_import_geometry", (p) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== p.doc_id) return t;
          if (p.deleted) {
            const next = { ...t.importGeometry };
            delete next[p.import];
            return { ...t, importGeometry: next };
          }
          const prev = t.importGeometry[p.import] ?? {
            glbB64: null,
            topology: null,
            errorMsg: null,
          };
          const loading = p.loading === true;
          return {
            ...t,
            importGeometry: {
              ...t.importGeometry,
              [p.import]: loading
                ? { ...prev, loading: true }
                : {
                    glbB64: p.glb_b64 ?? prev.glbB64,
                    topology: p.topology ?? prev.topology,
                    errorMsg: p.error ?? null,
                    loading: false,
                  },
            },
          };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<PlaywrightFrameEvent>("playwright_frame", (e) => {
      if (e.kind === "session_started") {
        setBrowserState((s) => ({
          ...s,
          active: true,
          url: e.url ?? s.url,
          title: e.title ?? s.title,
        }));
        // Auto-show the panel the first time a browser session opens.
        setBrowserCollapsed(false);
      } else if (e.kind === "session_ended") {
        setBrowserState((s) => ({ ...s, active: false }));
      } else if (e.kind === "navigated") {
        setBrowserState((s) => ({ ...s, url: e.url ?? s.url }));
      } else if (e.kind === "frame") {
        const frame: BrowserFrame = {
          data: e.data,
          mime: e.mime,
          deviceWidth: e.device_width,
          deviceHeight: e.device_height,
          scale: e.page_scale_factor,
        };
        // Don't flip `active` here — only session_started should do
        // that. Frames arrive while the agent is on about:blank too;
        // we want them buffered into state but not visible until the
        // session is announced.
        setBrowserState((s) => ({
          ...s,
          frame,
          lastFrameAt: Date.now(),
        }));
      }
    });
  }, []);

  useEffect(() => {
    return on<PermissionRequestEvent>("permission_request", (p) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== p.doc_id) return t;
          return { ...t, turns: appendPermissionBlock(t.turns, p) };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<PermissionResolvedEvent>("permission_resolved", (p) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== p.doc_id) return t;
          return { ...t, turns: updatePermissionStatus(t.turns, p) };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<ChatEvent>("chat_event", (e) => {
      setTabs((cur) =>
        cur.map((t) => {
          if (t.doc.id !== e.doc_id) return t;
          const next: TabState = { ...t, turns: applyChatEvent(t.turns, e) };
          // Mirror TodoWrite calls into per-tab `todos` so the chat panel
          // can render a live task list as the agent works.
          if (e.kind === "tool_use" && e.tool === "TodoWrite") {
            const input = (e.input ?? {}) as { todos?: unknown };
            const items = Array.isArray(input.todos) ? input.todos : [];
            next.todos = items
              .filter((it): it is Record<string, unknown> =>
                typeof it === "object" && it !== null,
              )
              .map((it) => ({
                content: String(it.content ?? ""),
                status:
                  it.status === "in_progress" || it.status === "completed"
                    ? it.status
                    : "pending",
                activeForm:
                  typeof it.activeForm === "string" ? it.activeForm : undefined,
              }));
          }
          return next;
        }),
      );
    });
  }, []);

  // --- keyboard shortcuts ---------------------------------------------

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const meta = e.ctrlKey || e.metaKey;
      if (!meta) return;
      const k = e.key.toLowerCase();
      if (k === "r") {
        e.preventDefault();
        refresh();
      } else if (k === "s") {
        e.preventDefault();
        save();
      } else if (k === "n") {
        e.preventDefault();
        setShowNew(true);
      } else if (k === "o") {
        e.preventDefault();
        setShowOpen(true);
      } else if (k === "w") {
        e.preventDefault();
        if (activeId) closeTab(activeId);
      } else if (e.key === ",") {
        e.preventDefault();
        setShowSettings(true);
      } else if (e.key === "Tab") {
        e.preventDefault();
        cycleTab(e.shiftKey ? -1 : 1);
      } else if (e.key >= "1" && e.key <= "9") {
        e.preventDefault();
        jumpTab(parseInt(e.key, 10) - 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [refresh, save, activeId, closeTab, cycleTab, jumpTab]);

  // --- contexts -------------------------------------------------------

  const isAgentRunning = useMemo(
    () =>
      activeTab?.turns.some(
        (t) => t.role === "assistant" && t.status === "running",
      ) ?? false,
    [activeTab],
  );

  const ui = useMemo(
    () => ({
      openNew: () => setShowNew(true),
      openOpen: () => setShowOpen(true),
      openSettings: () => setShowSettings(true),
    }),
    [],
  );

  const tabsCtx = useMemo(
    () => ({ tabs, activeId, addTab, closeTab, focusTab, cycleTab, jumpTab }),
    [tabs, activeId, addTab, closeTab, focusTab, cycleTab, jumpTab],
  );

  const chatCtx = useMemo(
    () => ({
      turns: activeTab?.turns ?? [],
      isAgentRunning,
      send,
      pendingAttachments: activeTab?.pendingAttachments ?? [],
      addAttachment,
      removeAttachment,
      todos: activeTab?.todos ?? [],
    }),
    [activeTab, isAgentRunning, send, addAttachment, removeAttachment],
  );

  const viewerCtx = useMemo(() => {
    if (!activeTab) {
      return {
        visible: [],
        visibleSketches: [],
        visibleImports: [],
        activeName: null,
        errorMsg: null,
      };
    }
    const objects = activeTab.doc.objects ?? [];
    const visible = objects
      .filter((o) => o.visible)
      .map((o) => ({
        name: o.name,
        geometry: activeTab.geometry[o.name] ?? {
          glbB64: null,
          topology: null,
          errorMsg: null,
        },
      }));
    const sketches = activeTab.doc.sketches ?? [];
    const visibleSketches = sketches
      .filter((s) => s.visible)
      .map((s) => ({
        name: s.name,
        geometry: activeTab.sketchGeometry[s.name] ?? {
          polylines: null,
          dimensions: null,
          plane: null,
          errorMsg: null,
        },
      }));
    const imports = activeTab.doc.imports ?? [];
    const visibleImports = imports
      .filter((i) => i.visible)
      .map((i) => ({
        name: i.name,
        geometry: activeTab.importGeometry[i.name] ?? {
          glbB64: null,
          topology: null,
          errorMsg: null,
        },
      }));
    const activeName = activeTab.doc.active_object ?? null;
    const errorMsg = activeName
      ? activeTab.geometry[activeName]?.errorMsg ?? null
      : null;
    return { visible, visibleSketches, visibleImports, activeName, errorMsg };
  }, [activeTab]);

  const browserCtx = useMemo<BrowserCtx>(
    () => ({
      ...browserState,
      collapsed: browserCollapsed,
      setCollapsed: setBrowserCollapsed,
    }),
    [browserState, browserCollapsed],
  );

  return (
    <UiContext.Provider value={ui}>
      <TabsContext.Provider value={tabsCtx}>
        <ChatContext.Provider value={chatCtx}>
          <DocContext.Provider value={{ doc, refresh }}>
            <ViewerContext.Provider value={viewerCtx}>
              <BrowserContext.Provider value={browserCtx}>
                {tabs.length > 0 ? (
                  <AppShell />
                ) : (
                  <Welcome
                    onNew={ui.openNew}
                    onOpen={ui.openOpen}
                    onSettings={ui.openSettings}
                  />
                )}
              <NewProjectDialog
                open={showNew}
                onClose={() => setShowNew(false)}
                onCreated={addTab}
              />
              <OpenProjectDialog
                open={showOpen}
                onClose={() => setShowOpen(false)}
                onOpened={addTab}
              />
              <SettingsDialog open={showSettings} onClose={() => setShowSettings(false)} />
              </BrowserContext.Provider>
            </ViewerContext.Provider>
          </DocContext.Provider>
        </ChatContext.Provider>
      </TabsContext.Provider>
    </UiContext.Provider>
  );
}

/**
 * Append a permission-pending block to the running assistant turn for
 * `msg_id`. If no turn exists yet (rare race), spin one up so the card
 * shows up regardless.
 */
function appendPermissionBlock(cur: Turn[], p: PermissionRequestEvent): Turn[] {
  const idx = cur.findIndex((t) => t.id === p.msg_id && t.role === "assistant");
  const base: Turn =
    idx >= 0
      ? cur[idx]
      : { id: p.msg_id, role: "assistant", blocks: [], status: "running" };
  const turn = base as Extract<Turn, { role: "assistant" }>;
  const block: ChatPermissionBlock = {
    kind: "permission",
    requestId: p.request_id,
    tool: p.tool,
    input: p.input,
    toolUseId: p.tool_use_id,
    status: "pending",
  };
  return upsertTurn(cur, { ...turn, blocks: [...turn.blocks, block] });
}

function updatePermissionStatus(cur: Turn[], p: PermissionResolvedEvent): Turn[] {
  return cur.map((t) => {
    if (t.role !== "assistant") return t;
    const blocks = t.blocks.map((b) => {
      if (b.kind !== "permission" || b.requestId !== p.request_id) return b;
      const status: ChatPermissionBlock["status"] = p.message?.includes("timed out")
        ? "timeout"
        : p.approved
          ? "approved"
          : "denied";
      return { ...b, status, message: p.message };
    });
    return { ...t, blocks };
  });
}

function applyChatEvent(cur: Turn[], e: ChatEvent): Turn[] {
  const idx = cur.findIndex((t) => t.id === e.msg_id && t.role === "assistant");
  const base: Turn =
    idx >= 0 ? cur[idx] : { id: e.msg_id, role: "assistant", blocks: [], status: "running" };
  const turn = base as Extract<Turn, { role: "assistant" }>;
  const blocks: ChatBlock[] = [...turn.blocks];

  switch (e.kind) {
    case "start":
      break;
    case "text": {
      const last = blocks[blocks.length - 1];
      if (last?.kind === "text") {
        blocks[blocks.length - 1] = { ...last, text: last.text + (e.text ?? "") };
      } else {
        blocks.push({ kind: "text", text: e.text ?? "" });
      }
      break;
    }
    case "tool_use":
      blocks.push({
        kind: "tool",
        tool: e.tool ?? "?",
        input: e.input,
        toolUseId: e.tool_use_id,
      });
      break;
    case "tool_result": {
      const target = [...blocks].reverse().find(
        (b): b is ChatToolBlock => b.kind === "tool" && b.toolUseId === e.tool_use_id,
      );
      if (target) {
        target.resultText = e.text;
        target.resultImages = e.images && e.images.length > 0 ? e.images : undefined;
        target.isError = e.is_error;
      }
      break;
    }
    case "error":
      return upsertTurn(cur, { ...turn, blocks, status: "error", errorText: e.text });
    case "done":
      return upsertTurn(cur, {
        ...turn,
        blocks,
        status: turn.status === "error" ? "error" : "done",
      });
    case "result":
      break;
  }
  return upsertTurn(cur, { ...turn, blocks });
}

function upsertTurn(turns: Turn[], turn: Turn): Turn[] {
  const idx = turns.findIndex((t) => t.id === turn.id);
  if (idx === -1) return [...turns, turn];
  const next = turns.slice();
  next[idx] = turn;
  return next;
}
