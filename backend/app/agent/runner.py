"""Run the Claude Agent for one chat turn against a project.

The agent sees the project directory as its working directory. Each
project has one or more *objects* under `objects/`, and exactly one is
*active* at a time. Read/Edit/Write the active object's script; CAD tools
operate on the active object automatically.
"""
from __future__ import annotations

import asyncio
import threading
import traceback
import uuid
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from .. import permissions, settings
from ..cad.project import Project
from ..cad.script_runner import RunResult
from ..events import bus
from .tools import (
    ALL_TOOL_NAMES,
    CadToolset,
    build_cad_server,
)


# Tool names the agent gets via @playwright/mcp. Lifted from the
# server's documented surface; we keep them here so allowed_tools is
# explicit and a future Playwright update doesn't silently change what
# the agent can do.
PLAYWRIGHT_TOOL_PREFIX = "mcp__playwright__"
PLAYWRIGHT_TOOL_NAMES = [
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_close",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_resize",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_console_messages",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_handle_dialog",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_evaluate",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_file_upload",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_install",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_press_key",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_navigate",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_navigate_back",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_network_requests",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_take_screenshot",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_snapshot",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_click",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_drag",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_hover",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_select_option",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_tabs",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_type",
    f"{PLAYWRIGHT_TOOL_PREFIX}browser_wait_for",
]


PLAYWRIGHT_PROMPT_BLOCK = """
PLAYWRIGHT BROWSER (experimental, enabled):
- mcp__playwright__browser_navigate / browser_click / browser_type /
  browser_take_screenshot / etc. give you a real Chromium browser.
  Use it for things WebFetch can't do: pages that need login, JS-heavy
  product configurators, downloads behind a click, etc.
- The user may have set "ask before each browser action" in Settings.
  When that's on, every Playwright tool call pauses and shows the user
  a permission card with the tool name and arguments. They can approve
  or deny. Don't take it personally if a request is denied — just pick
  a different approach.
- After landing on a page, ALWAYS take a screenshot or browser_snapshot
  before clicking — selectors based on visible content are far more
  reliable than guessing aria-labels.
- Close the browser when you're done with `browser_close` so it doesn't
  linger.
"""


SKETCHFAB_PROMPT_BLOCK = """
SKETCHFAB INTEGRATION (enabled):
- mcp__cad__sketchfab_search — search the public catalogue for a
  reference part by description; returns thumbnails you can SEE.
- mcp__cad__sketchfab_view — drill into one uid for a larger preview
  before committing to a download.
- mcp__cad__sketchfab_download — pull the best available source file
  (STEP > IGES > BREP > STL > GLB > glTF) into imports/. STEP/IGES/
  BREP downloads support full booleans + measurements; STL/GLB/glTF
  are mesh-only (bbox + viewer rendering, no booleans).
- Most Sketchfab models are mesh-only (Blender exports). Treat their
  bbox and the visible mesh as your measurement source; design the
  user's part around those dimensions, don't try to boolean against
  the mesh.
- Workflow: search → view → download → import_inspect → use bbox /
  visible reference when authoring the user's object.
- Always pass `downloadable_only=true` to the search when the user
  actually wants a usable reference (not just a thumbnail).
"""


SYSTEM_PROMPT_TEMPLATE = """You are a CAD design assistant inside Agent CAD, a parametric modeller built on CADQuery.

A PROJECT in Agent CAD contains three kinds of artifact:
  - OBJECTS — CADQuery scripts under `objects/<name>.py` that define a
    top-level `model` (a cq.Workplane). These are the actual 3D parts.
  - SKETCHES — CADQuery scripts under `sketches/<name>.py` that define a
    top-level `sketch` (a cq.Sketch) and optionally `plane` (a workplane
    spec). Sketches are 2D profiles that live on a named plane in 3D
    space; they are first-class artifacts that object scripts consume.
  - IMPORTS — user-supplied STEP files under `imports/<name>.step`. They
    are READ-ONLY reference geometry: a real solid the agent can measure
    off and boolean against, but never edit or recreate.

Exactly one artifact is the *active edit target* at a time — either an
object or a sketch. (Imports are never active — they're inputs, not
artifacts the agent authors.) The Tweaks panel and Read/Edit/Write/
run_model follow whichever editable artifact is active. The viewer
renders all visible objects + imports as 3D geometry, and all visible
sketches as line overlays on their declared planes.

Active edit target: **{active_kind}** {active_artifact}
All objects:  {all_objects}
All sketches: {all_sketches}
All imports:  {all_imports}
{requirements_section}

USE IMPORTS BEFORE REBUILDING:
  When the project has imports, treat them as the source of truth for
  any geometry they cover. The user added them so you don't have to
  reverse-engineer dimensions — measure off them with import_inspect or
  consume them directly via the injected `imports` dict (e.g.
  `model = base.cut(imports["bracket"])` for a clearance pocket,
  `imports["bracket"].faces(">Z").val().BoundingBox()` for a face's
  size). Do NOT recreate an imported part from scratch when it's already
  available — that's the bug we explicitly want to avoid.

SKETCH-FIRST WORKFLOW (the user expects this — don't skip):
  1. When the user asks for a part with a non-trivial 2D profile
     (anything more complex than a basic box / cylinder), START by
     calling create_sketch and authoring a fully-constrained 2D profile.
     Every dimension explicit (numeric or via params). Use
     .constrain(...).solve() if you need geometric constraints
     (coincident, parallel, perpendicular, distance, angle).
  2. snapshot_sketch to verify the profile looks right.
  3. set_active_object to flip the edit target back to the consuming
     object's script.
  4. In the object script, build the 3D geometry by referencing the
     sketch through the injected `sketches` dict — e.g.:
         model = sketches["base-profile"].extrude(20)
         model = sketches["rib"].sweep(sketches["spine-path"])
     Don't inline the 2D profile inside the object script when a sketch
     would express it more clearly.
  5. run_model and verify with snapshot.

The `sketches` dict is auto-injected into every object script — each
entry is a cq.Workplane already placed on the sketch's declared plane,
ready to .extrude() / .loft() / .sweep() / .placeSketch().

When the user asks for a *new* part (a separate body — e.g. "now design
a matching lid", "add a screw to hold this together"), call
create_object first; that creates a new seed script and makes it
active. When the user asks for a *change* to the existing thing, just
edit the active artifact. If you're unsure, ask.

Conventions:
- Units are millimetres unless the user says otherwise.
- An object script must define `model` (a cq.Workplane). It receives
  `params` (own dict) and `sketches` (project-wide dict, name → placed
  cq.Workplane).
- A sketch script must define `sketch` (a cq.Sketch) and optionally
  `plane`. Plane forms: "XY" / "XZ" / "YZ" / ("XY", offset_mm) / a full
  cq.Plane(...). It receives `params` (own dict).
- Read params with `params.get("name", default)`. Define new params via
  the set_parameter tool when the value is something the user is likely
  to tweak (length, wall thickness, hole radius, etc.). Each artifact
  has its own params namespace; set_parameter writes to whichever is
  active.
- Sketches must be fully constrained — every dimension explicit, no
  implicit defaults. Prefer .rect(L, W) / .circle(R) / .polyline([...])
  with concrete numbers or named params, plus .constrain().solve() for
  geometric relationships.
- After editing the active script, ALWAYS call mcp__cad__run_model
  to verify it works. For an object: produces geometry. For a sketch:
  tessellates without error. If it errors, fix and re-run.
- When you change geometry that you can't easily picture, call
  mcp__cad__snapshot with the relevant view ('iso','top','front','right',
  etc.) to actually see what you made. You ARE multimodal — use it.
- When the user attaches an annotated viewer screenshot, the image's
  description gives you the exact camera pose (position/target/up in
  CADQuery coords). To verify a fix, call snapshot with that pose as the
  `camera` argument — do NOT default to a preset preset like 'iso' or
  'top', because the user circled what they did *because* presets weren't
  showing it. Same goes for section_snapshot, scene_snapshot, and
  preview_boolean — all four accept an explicit `camera`.
- For internal features (pockets, holes, walls, ribs) the outside view
  hides what matters — use mcp__cad__section_snapshot to slice the part
  with an axis-aligned plane and look at the cross-section.
- For dimensions and topology questions, prefer the dedicated tools:
  measure (overall bbox/volume/area), mass_properties (centre of mass +
  inertia), query_faces / query_edges / query_vertices (per-entity info),
  check_validity (is the shape a well-formed solid?), and
  distance_between for the gap between any two entities — including two
  features of the SAME object: refs are 'name', 'name.face[i]',
  'name.edge[i]', 'name.vertex[i]', or '.face[i]' (active object).
  Use eval_expression as the escape hatch when none of those fit.
  Don't print() inside the script.
- For multi-object work: use scene_snapshot to render several objects
  together (lid-on-case, screw-in-hole) and preview_boolean to compute
  union/intersection/difference of two objects WITHOUT modifying their
  scripts — a non-empty intersection shows exactly where two parts
  collide.
- At the END of the turn, once the project is in a state worth saving,
  call mcp__cad__commit_turn with a short imperative subject summarising
  the change. This is the user's timeline — be descriptive but concise.
  Commits include all objects in the project.
- Keep narration brief between tool calls. The user can see your tool
  calls; they don't need a play-by-play.

Built-in tools you should reach for:
- WebSearch + WebFetch — when the user asks for a part to a real-world
  spec (M3 screw, USB-C connector, NEMA 17 stepper, common bearing,
  threaded insert, etc.) and you don't already know the exact dimension,
  LOOK IT UP. Search for the datasheet or standard, then fetch the
  authoritative source. Do NOT guess dimensions — guessing produces
  parts that don't fit, and the user has called this out as a recurring
  bug.
- TodoWrite — for any task that takes more than 2-3 tool calls, use
  TodoWrite up front to lay out the steps, then mark each completed as
  you finish. The user sees the live task list in the chat sidebar so
  they can track progress.
- AskUserQuestion — when a real ambiguity exists (which face? what
  thickness? which standard?), ASK rather than guess. The chat surfaces
  these questions prominently. Reserve this for genuine ambiguity, not
  routine choices you can make and surface as commit messages.

When the user clicks on the model, you'll see a message that starts with
something like:
  [The user pointed at edge index 7 (geomType: CIRCLE) of the current model,
   world coordinates (15.0, 0.0, 7.5) mm.]
The pin is for the *active* object. The index is positional in
shape.Faces() / shape.Edges() / shape.Vertices() of the most recent run —
re-run the model first if you need it to be fresh. For edges and
vertices, prefer eval_expression for inspection
("model.val().Edges()[7].Length()", etc.). If the index seems stale
because the model has changed, fall back to locating the entity by world
coordinates (it will be near the (x, y, z) you were given).

Available CADQuery tips:
- Sketch on a plane: cq.Workplane("XY") (or "XZ", "YZ", or .workplane(offset=...))
- Common selectors: ">Z" top face, "<Z" bottom, ">X" rightmost, "%CIRCLE" circular edges
- Operations: .extrude(h), .cut(other), .union(other), .fillet(r), .chamfer(d), .shell(t)
- Sketch helpers: .rect(L, W), .circle(R), .polyline([(x,y),...]).close()
- For features that need to reference back to faces, use .tag("name") on
  the workplane stack and select with .faces(tag="name") later.
"""


def _build_requirements_section(project: Project) -> str:
    """User-defined requirements per object, with an instruction to verify
    after each change. Empty when no object has any requirements yet."""
    objs = project.list_objects()
    blocks: list[str] = []
    for o in objs:
        reqs = project.list_requirements(o["name"])
        if not reqs:
            continue
        bullets = "\n".join(f"  {i + 1}. {r}" for i, r in enumerate(reqs))
        blocks.append(f"- **{o['name']}**:\n{bullets}")
    if not blocks:
        return ""
    body = "\n".join(blocks)
    return (
        "\nREQUIREMENTS (user-defined, ordered):\n"
        f"{body}\n"
        "\n"
        "These are hard constraints the user expects each object to satisfy. "
        "After every change you make to an object, verify that ALL of its "
        "requirements still hold — use measure / mass_properties / "
        "query_faces / query_edges / eval_expression / section_snapshot as "
        "needed to check. If a change would violate a requirement, prefer to "
        "find an alternative that satisfies it. If you genuinely cannot, "
        "STOP, and tell the user which requirement is violated and why before "
        "committing.\n"
    )


def _build_system_prompt(project: Project) -> str:
    objs = [o["name"] for o in project.list_objects()]
    sketches = [s["name"] for s in project.list_sketches()]
    imports = [i["name"] for i in project.list_imports()]
    kind, name = project.active_artifact()
    body = SYSTEM_PROMPT_TEMPLATE.format(
        active_kind=kind,
        active_artifact=name,
        all_objects=", ".join(objs) if objs else "(none yet)",
        all_sketches=", ".join(sketches) if sketches else "(none yet)",
        all_imports=", ".join(imports) if imports else "(none yet)",
        requirements_section=_build_requirements_section(project),
    )
    s = settings.load()
    if s.sketchfab_enabled and s.sketchfab_token:
        body += SKETCHFAB_PROMPT_BLOCK
    if s.playwright_enabled:
        body += PLAYWRIGHT_PROMPT_BLOCK
    return body


def _make_permission_callback(project: Project, msg_id: str):
    """Build a can_use_tool callback that auto-allows our own CAD tools
    + standard editor tools, and routes Playwright (or any other) tool
    calls through the user via the chat permission card."""

    async def callback(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ):
        # Always-allow surface: our own CAD tools and the SDK's safe
        # built-ins. Anything else (currently just Playwright) needs
        # explicit user approval.
        if (
            tool_name.startswith("mcp__cad__")
            or tool_name in ("Read", "Write", "Edit", "Glob", "Grep",
                             "WebSearch", "WebFetch", "TodoWrite",
                             "AskUserQuestion")
        ):
            return PermissionResultAllow()

        request_id, ev = permissions.store.request()
        bus.emit("permission_request", {
            "doc_id": project.id,
            "msg_id": msg_id,
            "request_id": request_id,
            "tool": tool_name,
            "input": _safe(tool_input),
            "tool_use_id": context.tool_use_id,
        })
        # Wait off the asyncio loop so the permission UI can run.
        # Five-minute timeout — long enough for the user to read carefully,
        # short enough that an abandoned tab doesn't pin a worker forever.
        got_signal = await asyncio.to_thread(ev.wait, 300)
        if not got_signal:
            permissions.store.cancel(request_id)
            bus.emit("permission_resolved", {
                "doc_id": project.id,
                "msg_id": msg_id,
                "request_id": request_id,
                "approved": False,
                "message": "request timed out",
            })
            return PermissionResultDeny(message="permission request timed out")

        result = permissions.store.take_result(request_id)
        approved = bool(result and result.approved)
        bus.emit("permission_resolved", {
            "doc_id": project.id,
            "msg_id": msg_id,
            "request_id": request_id,
            "approved": approved,
            "message": result.message if result else "",
        })
        if approved:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(result.message if result else "denied by user") or "denied by user",
        )

    return callback


def run_chat_turn(
    project: Project,
    *,
    prompt: str,
    on_run: Callable[[RunResult], None],
    attachments: list[dict] | None = None,
    msg_id: str | None = None,
) -> None:
    """Fire-and-forget agent invocation. Progress streams via bus.emit."""
    msg_id = msg_id or f"msg_{uuid.uuid4().hex[:8]}"
    toolset = CadToolset(project, on_run)

    def _worker():
        try:
            asyncio.run(_run(project, toolset, prompt, attachments, msg_id))
        except Exception as e:
            bus.emit("chat_event", {
                "doc_id": project.id,
                "msg_id": msg_id,
                "kind": "error",
                "text": f"agent error: {e}\n{traceback.format_exc()}",
            })
        finally:
            bus.emit("chat_event", {"doc_id": project.id, "msg_id": msg_id, "kind": "done"})
            bus.emit("project_state", {"doc_id": project.id, "state": project.to_json()})

    threading.Thread(target=_worker, name="cad-agent", daemon=True).start()


async def _run(project: Project, toolset: CadToolset, prompt: str,
               attachments: list[dict] | None, msg_id: str) -> None:
    server = build_cad_server(toolset)
    user_settings = settings.load()

    mcp_servers: dict[str, Any] = {"cad": server}
    allowed_tools = list(ALL_TOOL_NAMES)
    if user_settings.playwright_enabled:
        mcp_servers["playwright"] = {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@playwright/mcp@latest"],
        }
        allowed_tools = list(allowed_tools) + PLAYWRIGHT_TOOL_NAMES

    # When the user wants permission prompts, install a can_use_tool
    # callback that routes through the chat. CAD + safe builtins are
    # auto-allowed inside the callback. The SDK requires streaming-mode
    # input when can_use_tool is set, so we always wrap the prompt
    # below.
    can_use_tool = None
    if user_settings.playwright_enabled and user_settings.playwright_require_permission:
        can_use_tool = _make_permission_callback(project, msg_id)

    options = ClaudeAgentOptions(
        cwd=str(project.path),
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        system_prompt=_build_system_prompt(project),
        permission_mode="bypassPermissions",
        model=user_settings.model,
        effort=user_settings.effort,  # type: ignore[arg-type]
        can_use_tool=can_use_tool,
    )

    bus.emit("chat_event", {"doc_id": project.id, "msg_id": msg_id, "kind": "start"})

    # Streaming-mode input is required when can_use_tool is set OR when
    # the prompt has image attachments. Otherwise pass a plain string.
    query_prompt: Any
    if can_use_tool is not None or attachments:
        query_prompt = _stream_multimodal_prompt(prompt, attachments or [])
    else:
        query_prompt = prompt

    async for message in query(prompt=query_prompt, options=options):
        # Tool USE blocks ride on AssistantMessage; tool RESULT blocks ride on
        # UserMessage (the SDK feeds tool output back as the next user turn,
        # mirroring Anthropic's API). We have to inspect both.
        if isinstance(message, (AssistantMessage, UserMessage)):
            content = message.content
            if isinstance(content, str):
                continue
            for block in content:
                if isinstance(block, TextBlock):
                    if isinstance(message, AssistantMessage):
                        bus.emit("chat_event", {
                            "doc_id": project.id, "msg_id": msg_id,
                            "kind": "text", "text": block.text,
                        })
                elif isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                    bus.emit("chat_event", {
                        "doc_id": project.id, "msg_id": msg_id,
                        "kind": "tool_use",
                        "tool": block.name,
                        "input": _safe(block.input),
                        "tool_use_id": block.id,
                    })
                elif isinstance(block, (ToolResultBlock, ServerToolResultBlock)):
                    bus.emit("chat_event", {
                        "doc_id": project.id, "msg_id": msg_id,
                        "kind": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "is_error": getattr(block, "is_error", False),
                        "text": _block_text(block.content),
                        "images": _block_images(block.content),
                    })
        elif isinstance(message, ResultMessage):
            bus.emit("chat_event", {
                "doc_id": project.id, "msg_id": msg_id,
                "kind": "result",
                "subtype": getattr(message, "subtype", None),
                "is_error": getattr(message, "is_error", False),
            })


async def _stream_multimodal_prompt(text: str, attachments: list[dict]):
    """Yield a single user message with text + image content blocks.

    The SDK's streaming-mode input expects Anthropic-shaped image blocks
    (`{type:"image", source:{type:"base64", media_type, data}}`); the
    frontend hands us MCP-shaped `{data, mimeType}` so we translate here.
    """
    content: list[dict] = []
    if text:
        content.append({"type": "text", "text": text})
    for att in attachments:
        data = att.get("data")
        mime = att.get("mimeType") or att.get("media_type") or "image/png"
        if not data:
            continue
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": data},
        })
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
        "session_id": "",
    }


def _safe(obj: Any) -> Any:
    try:
        import json
        json.dumps(obj)
        return obj
    except Exception:
        return str(obj)


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
            elif hasattr(c, "text") and not hasattr(c, "data"):
                parts.append(getattr(c, "text", "") or "")
        return "".join(parts)
    return str(content)


def _block_images(content: Any) -> list[dict]:
    """Extract any image content from a tool result.

    Tools (ours and others') may return image content in either the MCP
    shape `{type:"image", data, mimeType}` or the Anthropic shape
    `{type:"image", source:{type:"base64", media_type, data}}`. We also
    handle ImageContent dataclass instances from the SDK.
    """
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for c in content:
        data = None
        mime = None
        if isinstance(c, dict) and c.get("type") == "image":
            data = c.get("data")
            mime = c.get("mimeType") or c.get("media_type")
            if not data and isinstance(c.get("source"), dict):
                src = c["source"]
                data = src.get("data")
                mime = src.get("media_type") or mime
        elif hasattr(c, "data") and (hasattr(c, "mimeType") or hasattr(c, "media_type")):
            data = getattr(c, "data", None)
            mime = getattr(c, "mimeType", None) or getattr(c, "media_type", None)
        if data:
            out.append({"data": data, "mimeType": mime or "image/png"})
    return out
