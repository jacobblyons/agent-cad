/**
 * Display-only unit selection. Backend geometry is always millimetres
 * (CADQuery's native unit) — this module only changes how lengths are
 * RENDERED in the viewer (sketch dimension labels, pin coordinates).
 *
 * Storage is a single localStorage key + a tiny pub-sub so multiple
 * components stay in sync without prop-drilling. Default is "mm".
 */
import { useEffect, useState } from "react";

export type Unit = "mm" | "in";

const STORAGE_KEY = "agent-cad:units";
const MM_PER_INCH = 25.4;

function readInitial(): Unit {
  if (typeof window === "undefined") return "mm";
  const v = window.localStorage.getItem(STORAGE_KEY);
  return v === "in" ? "in" : "mm";
}

let currentUnit: Unit = readInitial();
const subscribers = new Set<(u: Unit) => void>();

export function getUnit(): Unit {
  return currentUnit;
}

export function setUnit(u: Unit): void {
  if (u === currentUnit) return;
  currentUnit = u;
  if (typeof window !== "undefined") {
    window.localStorage.setItem(STORAGE_KEY, u);
  }
  for (const s of subscribers) s(u);
}

/** React hook: returns the active unit and re-renders when it changes. */
export function useUnit(): Unit {
  const [u, setU] = useState<Unit>(currentUnit);
  useEffect(() => {
    const cb = (next: Unit) => setU(next);
    subscribers.add(cb);
    return () => {
      subscribers.delete(cb);
    };
  }, []);
  return u;
}

/** Format a millimetre value for display in the active unit.
 *
 * - mm: no suffix (matches typical CAD display where mm is the assumed
 *       default and an explicit "mm" is visual noise).
 * - in: trailing inch mark (`"`), so the unit is unambiguous.
 *
 * Precision: 2 decimals for small values, 1 for larger — same heuristic
 * the dimension labels were already using for mm. */
export function formatLength(mm: number, unit: Unit = currentUnit): string {
  if (unit === "in") {
    const v = mm / MM_PER_INCH;
    const text = Math.abs(v) < 1 ? v.toFixed(3) : v.toFixed(2);
    return `${text}"`;
  }
  return Math.abs(mm) < 10 ? mm.toFixed(2) : mm.toFixed(1);
}
