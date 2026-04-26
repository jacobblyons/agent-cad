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
};

export type DocSummary = {
  id: string;          // absolute path to the project directory
  path: string;
  title: string;
  head_sha: string;
  head_branch: string;
  uncommitted: boolean;
  commits: Commit[];
  objects: ObjectMeta[];
  active_object: string;
  // Active object's params, surfaced for the Tweaks panel.
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
