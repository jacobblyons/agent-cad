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

export type ChatImage = {
  data: string;
  mimeType: string;
  /**
   * Where the image came from. The agent needs this so it doesn't mistake a
   * user's drawing for a render of the model.
   *  - "drawing" — freehand sketch on a blank canvas
   *  - "snapshot" — viewer screenshot the user drew on top of
   */
  source?: "drawing" | "snapshot";
  /** Free-form context (e.g. camera position) appended to the prompt preamble. */
  description?: string;
};

/**
 * One item in the agent's TodoWrite task list. Whenever the agent calls
 * the TodoWrite tool, the latest list is mirrored into the active tab's
 * `todos` so the UI can render real-time progress.
 */
export type AgentTodo = {
  content: string;
  status: "pending" | "in_progress" | "completed";
  /** Present-continuous form ("Loading the bracket model"); falls back to content. */
  activeForm?: string;
};

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
/**
 * Permission ask the agent is blocked on. The card renders Approve /
 * Deny buttons; once the user clicks one we transition to status
 * "approved" or "denied" and the agent's can_use_tool callback resumes.
 */
export type ChatPermissionBlock = {
  kind: "permission";
  requestId: string;
  tool: string;
  input: unknown;
  toolUseId?: string;
  status: "pending" | "approved" | "denied" | "timeout";
  message?: string;
};
export type ChatBlock = ChatTextBlock | ChatToolBlock | ChatPermissionBlock;

export type Turn =
  | { id: string; role: "user"; text: string; images?: ChatImage[] }
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
  send: (text: string, opts?: { pin?: PinInfo; images?: ChatImage[] }) => Promise<void>;
  /** Queue of attachments staged for the next send. */
  pendingAttachments: ChatImage[];
  addAttachment: (img: ChatImage) => void;
  removeAttachment: (index: number) => void;
  /** Most recent task list the agent published via TodoWrite. */
  todos: AgentTodo[];
};

export const ChatContext = createContext<ChatCtx>({
  turns: [],
  isAgentRunning: false,
  send: async () => {},
  pendingAttachments: [],
  addAttachment: () => {},
  removeAttachment: () => {},
  todos: [],
});

export const useChat = () => useContext(ChatContext);

export function formatPinForUser(pin: PinInfo, text: string): string {
  const idx = pin.entity_index ?? "?";
  return `📍 ${pin.entity_kind} ${idx} — ${text}`;
}

/**
 * Wrap the prompt with a preamble that names each attached image and where it
 * came from. The agent needs this because:
 *   1. annotated snapshots look like the model but contain user pen marks the
 *      model doesn't actually have, and
 *   2. freehand drawings have no scene context at all — without a label the
 *      agent can read them as schematic-of-the-model rather than user input.
 *
 * Image order in the preamble must match the order images are attached to
 * the API message — runner.py emits text first, then images in array order.
 */
export function formatAttachmentsForPrompt(images: ChatImage[], text: string): string {
  if (images.length === 0) return text;
  const lines = images.map((img, i) => {
    const n = i + 1;
    if (img.source === "snapshot") {
      const ctx = img.description ? ` ${img.description}` : "";
      return (
        `Image ${n}: an annotated screenshot of the current 3D viewer. The user ` +
        `drew on top of it to point at features — those pen marks are *not* part ` +
        `of the model.${ctx}`
      );
    }
    if (img.source === "drawing") {
      return (
        `Image ${n}: a freehand sketch the user drew on a blank canvas to ` +
        `illustrate intent. Treat it as a hand-drawn hint, not a render of the model.`
      );
    }
    return `Image ${n}: a user-supplied image.`;
  });
  return `[Attached images:\n${lines.join("\n")}]\n\n${text}`;
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
