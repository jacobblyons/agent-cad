/**
 * Chat state lifted to App so both ChatPanel and ViewerPane can write to it.
 *
 * Pin info is the lightweight "user pointed here" annotation that the viewer
 * builds when the user clicks on a face in Annotate mode. It rides with the
 * outgoing chat prompt (formatted into a context line) and into the local
 * user-visible bubble (formatted as a 📍 prefix). Nothing about the pin is
 * persisted — the agent reads it once, addresses it, and it's gone.
 */
import { createContext, useContext } from "react";

export type EntityKind = "face" | "edge" | "vertex";

export type PinInfo = {
  entity_kind: EntityKind;
  entity_index: number | null;
  /** geomType from CADQuery — "PLANE" / "CYLINDER" / "LINE" / "CIRCLE" etc. */
  entity_type?: string | null;
  pin_world: [number, number, number];
};

export type ChatImage = { data: string; mimeType: string };

export type ChatTextBlock = { kind: "text"; text: string };
export type ChatToolBlock = {
  kind: "tool";
  tool: string;
  input: unknown;
  resultText?: string;
  resultImages?: ChatImage[];
  isError?: boolean;
  toolUseId?: string;
};
export type ChatBlock = ChatTextBlock | ChatToolBlock;

export type Turn =
  | { id: string; role: "user"; text: string }
  | {
      id: string;
      role: "assistant";
      blocks: ChatBlock[];
      status: "running" | "done" | "error";
      errorText?: string;
    };

export type ChatCtx = {
  turns: Turn[];
  isAgentRunning: boolean;
  send: (text: string, pin?: PinInfo) => Promise<void>;
};

export const ChatContext = createContext<ChatCtx>({
  turns: [],
  isAgentRunning: false,
  send: async () => {},
});

export const useChat = () => useContext(ChatContext);

export function formatPinForUser(pin: PinInfo, text: string): string {
  const idx = pin.entity_index ?? "?";
  return `📍 ${pin.entity_kind} ${idx} — ${text}`;
}

export function formatPinForPrompt(pin: PinInfo, text: string): string {
  const [x, y, z] = pin.pin_world.map((n) => n.toFixed(2));
  const idx = pin.entity_index ?? "(unknown)";
  const typeNote = pin.entity_type ? ` (geomType: ${pin.entity_type})` : "";
  const accessor = pin.entity_kind === "face"
    ? "shape.Faces()"
    : pin.entity_kind === "edge"
    ? "shape.Edges()"
    : "shape.Vertices()";
  return (
    `[The user pointed at ${pin.entity_kind} index ${idx}${typeNote} of the ` +
    `current model, world coordinates (${x}, ${y}, ${z}) mm.]\n\n` +
    `${text}\n\n` +
    `(The index refers to position in ${accessor} of the most recent run; ` +
    `if you've changed the model since, prefer locating the entity by ` +
    `coordinates. Use snapshot/measure/query_faces/eval_expression to ` +
    `inspect further.)`
  );
}
