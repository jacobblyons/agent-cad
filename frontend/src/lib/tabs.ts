/**
 * Multi-tab state.
 *
 * App owns a Map<docId, TabState> and an activeId. Backend events arrive
 * with a doc_id and get routed into the right tab's slice. Existing
 * DocContext / ChatContext / ViewerContext re-derive from the active tab,
 * so individual panels don't need to know about tabs at all.
 */
import { createContext, useContext } from "react";
import type { ChatImage, Turn } from "./chat";
import type { DocSummary } from "./doc";
import type { ObjectGeometry } from "./viewer";

export type TabState = {
  doc: DocSummary;
  turns: Turn[];
  // Per-object geometry, keyed by object name. Slots persist across visibility
  // toggles so hiding+showing doesn't force a re-render server-side.
  geometry: Record<string, ObjectGeometry>;
  // Attachments queued for the next chat send (sketches, viewer snapshots).
  // Cleared after a successful send.
  pendingAttachments: ChatImage[];
};

export type TabsCtx = {
  tabs: TabState[];                          // ordered, oldest-added first
  activeId: string | null;
  addTab: (doc: DocSummary) => void;
  closeTab: (id: string) => void;
  focusTab: (id: string) => void;
  cycleTab: (delta: 1 | -1) => void;
  jumpTab: (index: number) => void;
};

export const TabsContext = createContext<TabsCtx>({
  tabs: [],
  activeId: null,
  addTab: () => {},
  closeTab: () => {},
  focusTab: () => {},
  cycleTab: () => {},
  jumpTab: () => {},
});

export const useTabs = () => useContext(TabsContext);
