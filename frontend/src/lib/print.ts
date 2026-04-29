/**
 * Print-phase state shared between the chat panel, the print pane, and
 * the AppShell mode switch. The backend owns the canonical session
 * (preset / overrides / last slice / printer); the frontend mirrors
 * it per-tab and drives transitions via JsApi calls.
 */
import { createContext, useContext } from "react";

export type SliceOverride = {
  key: string;
  value: string;
  note?: string;
};

export type SliceResult = {
  ok: boolean;
  error?: string | null;
  sliced_path?: string | null;
  sliced_format?: string | null;
  estimated_minutes?: number | null;
  estimated_filament_g?: number | null;
  estimated_filament_m?: number | null;
  log?: string;
};

export type PrintSession = {
  project_id: string;
  preset: string;
  overrides: SliceOverride[];
  last_slice: SliceResult | null;
  last_export_path: string | null;
  printer_id: string | null;
  last_send_message: string;
  last_send_ok: boolean | null;
  started_at: number;
};

export type PrinterSummary = {
  id: string;
  name: string;
  kind: string;
  ip: string;
  serial: string;
  has_access_code: boolean;
  printer_profile: string;
  process_profile: string;
  filament_profile: string;
};

export type PresetInfo = {
  id: string;
  label: string;
  description: string;
};

export type PrintCtx = {
  /** Whether the active project is in the print phase. */
  active: boolean;
  /** Live session, or null when not in the phase. */
  session: PrintSession | null;
  printers: PrinterSummary[];
  defaultPrinterId: string;
  presets: PresetInfo[];
  /** Whether a slicer / send call is in flight. */
  busy: boolean;
  enter: (preset?: string) => Promise<void>;
  leave: () => Promise<void>;
  setPreset: (preset: string) => Promise<void>;
  setPrinter: (printerId: string) => Promise<void>;
  setOverrides: (overrides: SliceOverride[]) => Promise<void>;
  slice: () => Promise<void>;
  send: () => Promise<void>;
};

export const PrintContext = createContext<PrintCtx>({
  active: false,
  session: null,
  printers: [],
  defaultPrinterId: "",
  presets: [],
  busy: false,
  enter: async () => {},
  leave: async () => {},
  setPreset: async () => {},
  setPrinter: async () => {},
  setOverrides: async () => {},
  slice: async () => {},
  send: async () => {},
});

export function usePrint() {
  return useContext(PrintContext);
}

export function fmtMinutes(mins: number | null | undefined): string {
  if (mins == null) return "—";
  const m = Math.round(mins);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function fmtGrams(g: number | null | undefined): string {
  if (g == null) return "—";
  return `${g.toFixed(0)} g`;
}
