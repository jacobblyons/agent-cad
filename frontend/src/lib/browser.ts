/**
 * Live state of the embedded Chromium that the agent's Playwright tools
 * drive. The backend streams CDP `Page.screencastFrame` events; the
 * latest frame plus a small amount of metadata lives here so any panel
 * can subscribe.
 *
 * The browser is process-wide (one Chromium per app session, shared
 * across tabs), so this state is app-level rather than per-tab.
 */
import { createContext, useContext } from "react";

export type BrowserFrame = {
  data: string;          // base64-encoded JPEG
  mime: string;          // typically "image/jpeg"
  deviceWidth?: number;
  deviceHeight?: number;
  scale?: number;
};

export type BrowserState = {
  /** A page session has been opened (frames are or will be flowing). */
  active: boolean;
  /** Most recent navigation URL — surfaced as the panel's "address bar". */
  url: string | null;
  title: string | null;
  /** Latest frame received. Replaced on every Page.screencastFrame. */
  frame: BrowserFrame | null;
  /** Last-frame timestamp (ms) — UI uses it to fade to "stale" eventually. */
  lastFrameAt: number | null;
};

export type BrowserCtx = BrowserState & {
  /** Hide the panel without stopping the agent's browser session. */
  collapsed: boolean;
  setCollapsed: (v: boolean) => void;
};

export const BrowserContext = createContext<BrowserCtx>({
  active: false,
  url: null,
  title: null,
  frame: null,
  lastFrameAt: null,
  collapsed: true,
  setCollapsed: () => {},
});

export const useBrowser = () => useContext(BrowserContext);
