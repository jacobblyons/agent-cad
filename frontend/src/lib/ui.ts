/**
 * Lightweight UI dialog state — App owns the open/closed flags and
 * exposes openers via context so any panel can trigger them.
 */
import { createContext, useContext } from "react";

export type UiCtx = {
  openNew: () => void;
  openOpen: () => void;
  openSettings: () => void;
};

export const UiContext = createContext<UiCtx>({
  openNew: () => {},
  openOpen: () => {},
  openSettings: () => {},
});

export function useUi() {
  return useContext(UiContext);
}
