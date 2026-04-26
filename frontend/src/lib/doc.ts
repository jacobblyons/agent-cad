/**
 * Project state shared across panels via React context.
 *
 * "doc" is a leftover name from the pre-git design — it now refers to a
 * project (a directory + git repo on disk).
 */
import { createContext, useContext } from "react";

export type Commit = {
  sha: string;
  short: string;
  subject: string;
  body: string;
  author: string;
  date: string;
};

export type ObjectMeta = {
  name: string;
  modified: number;
  visible: boolean;
  /** User-defined ordered list of hard constraints the agent must satisfy. */
  requirements: string[];
};

export type SketchMeta = {
  name: string;
  modified: number;
  visible: boolean;
};

export type ImportMeta = {
  name: string;
  ext: string;            // "step" or "stp"
  modified: number;
  size_bytes: number;
  visible: boolean;
};

/** Whether the active edit target is an object or a sketch. */
export type ActiveKind = "object" | "sketch";

export type DocSummary = {
  id: string;          // absolute path to the project directory
  path: string;
  title: string;
  head_sha: string;
  head_branch: string;
  uncommitted: boolean;
  commits: Commit[];
  objects: ObjectMeta[];
  sketches: SketchMeta[];
  imports: ImportMeta[];
  active_object: string;
  active_sketch: string | null;
  active_kind: ActiveKind;
  /** Active artifact's params (object or sketch), surfaced for the Tweaks panel. */
  params: Record<string, number>;
};

export type DocCtx = {
  doc: DocSummary | null;
  refresh: () => Promise<void>;
};

export const DocContext = createContext<DocCtx>({ doc: null, refresh: async () => {} });

export function useDoc(): DocCtx {
  return useContext(DocContext);
}

export function base64ToArrayBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}
