import { useCallback, useEffect, useMemo, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { NewProjectDialog } from "@/components/NewProjectDialog";
import { OpenProjectDialog } from "@/components/OpenProjectDialog";
import { SettingsDialog } from "@/components/SettingsDialog";
import { Welcome } from "@/components/Welcome";
import {
  ChatContext,
  formatPinForPrompt,
  formatPinForUser,
  type ChatBlock,
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
      return [...cur, { doc: d, turns: [], geometry: {} }];
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
    async (text: string, pin?: PinInfo) => {
      if (!doc || !text.trim()) return;
      const display = pin ? formatPinForUser(pin, text) : text;
      const prompt = pin ? formatPinForPrompt(pin, text) : text;
      const turn: Turn = { id: `u_${Date.now()}`, role: "user", text: display };
      setTabs((cur) =>
        cur.map((t) =>
          t.doc.id === doc.id ? { ...t, turns: [...t.turns, turn] } : t,
        ),
      );
      await call("chat_send", doc.id, prompt, []);
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
          return {
            ...t,
            geometry: {
              ...t.geometry,
              [p.object]: {
                glbB64: p.glb_b64 ?? prev.glbB64,
                topology: p.topology ?? prev.topology,
                errorMsg: p.error ?? null,
              },
            },
          };
        }),
      );
    });
  }, []);

  useEffect(() => {
    return on<ChatEvent>("chat_event", (e) => {
      setTabs((cur) =>
        cur.map((t) =>
          t.doc.id === e.doc_id ? { ...t, turns: applyChatEvent(t.turns, e) } : t,
        ),
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
    () => ({ turns: activeTab?.turns ?? [], isAgentRunning, send }),
    [activeTab, isAgentRunning, send],
  );

  const viewerCtx = useMemo(() => {
    if (!activeTab) {
      return { visible: [], activeName: null, errorMsg: null };
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
    const activeName = activeTab.doc.active_object ?? null;
    const errorMsg = activeName
      ? activeTab.geometry[activeName]?.errorMsg ?? null
      : null;
    return { visible, activeName, errorMsg };
  }, [activeTab]);

  return (
    <UiContext.Provider value={ui}>
      <TabsContext.Provider value={tabsCtx}>
        <ChatContext.Provider value={chatCtx}>
          <DocContext.Provider value={{ doc, refresh }}>
            <ViewerContext.Provider value={viewerCtx}>
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
            </ViewerContext.Provider>
          </DocContext.Provider>
        </ChatContext.Provider>
      </TabsContext.Provider>
    </UiContext.Provider>
  );
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
