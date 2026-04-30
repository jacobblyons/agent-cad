"""Standalone stdio MCP server exposing the Agent CAD tool surface.

Same tool implementations as the in-process server used by the desktop
app's chat agent — only the transport differs. This server speaks the
MCP stdio protocol so Claude Code (and any other MCP client) can drive
Agent CAD without the GUI.

State model:
  - The server is single-project: at most one Project is "open" at a
    time. Tools that operate on the project (run_model, snapshot,
    measure, …) act on that one. Use `open_project`, `create_project`,
    or `list_projects` to manage which project is current.
  - The print-phase tools require an explicit `enter_print_phase` call
    first. While in the print phase, slice / send / preset tools work;
    `leave_print_phase` returns to plain CAD mode. The desktop app's UI
    flag ("am I in the print phase?") is held in this server's state so
    the agent's behaviour mirrors the GUI's.

Usage (when launched by Claude Code via .mcp.json):
    python -m app.agent.standalone_server
    (the launcher in backend/scripts/mcp_server.py self-bootstraps into
    .venv/bin/python on macOS / Linux or .venv\\Scripts\\python.exe on
    Windows, so callers don't need to know the venv path.)

Optional CLI flags:
    --project <path>   open a project at startup (otherwise call
                       open_project from the agent side)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

# Make the `app` package importable however we're invoked: as a module
# from within backend/ (preferred), or as a script when Claude Code
# spawns us via an absolute path.
HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[2]   # .../backend
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp.types import (  # noqa: E402
    ImageContent,
    TextContent,
    Tool as McpTool,
)

from app import settings  # noqa: E402
from app.agent.tools import (  # noqa: E402
    BUILTIN_TOOLS,
    CadToolset,
    PrintToolset,
    build_cad_tools,
    build_print_tools,
)
from app.cad.project import (  # noqa: E402
    Project,
    list_recent,
    sanitize_object_name,
)
from app.events import bus  # noqa: E402
from app.printing import PRESETS  # noqa: E402
from app.printing.camera import grab_frame as grab_camera_frame  # noqa: E402
from app.printing.presets import lookup as lookup_preset  # noqa: E402
from app.printing.state import PhaseState  # noqa: E402

SERVER_NAME = "agent-cad"


# --------------------------------------------------------------------- #
# Disable bus event emission                                             #
# --------------------------------------------------------------------- #
#
# The CAD tools call bus.emit("doc_geometry", …) etc. assuming a
# webview is listening. With no webview attached the events would just
# pile up in the bus's queue forever. Replace the emit method with a
# silent no-op for this process so events get dropped on the floor.

def _noop_emit(channel: str, payload: Any) -> None:  # noqa: ARG001
    return None


bus.emit = _noop_emit  # type: ignore[assignment]


# --------------------------------------------------------------------- #
# Per-process state                                                      #
# --------------------------------------------------------------------- #


class _ServerState:
    """Mutable singleton holding the current project + phase state.

    `cad_toolset` and `print_toolset` are built once and reuse a
    mutable `.project` reference, so switching projects via
    `open_project` doesn't require re-registering tools.
    """

    def __init__(self) -> None:
        self.project: Project | None = None
        self.cad_toolset: CadToolset | None = None
        self.print_toolset: PrintToolset | None = None
        self.phase_state = PhaseState()
        self._render_callback = lambda _result: None

    def _render(self, result):
        # Object-script run results are pushed to the viewer in the
        # in-process harness; here they're just discarded — the agent
        # reads geometry / mass / measure via the dedicated tools.
        pass

    def set_project(self, project: Project) -> None:
        # Leave any active print phase from the previous project so we
        # don't lose track of which session belongs to which project.
        if self.project is not None:
            self.phase_state.leave(self.project.id)
        self.project = project
        if self.cad_toolset is None:
            self.cad_toolset = CadToolset(project, self._render)
            self.print_toolset = PrintToolset(project, _PrintApiAdapter(self))
        else:
            self.cad_toolset.project = project
            self.cad_toolset.invalidate()
            assert self.print_toolset is not None
            self.print_toolset.project = project

    def require_project(self) -> Project:
        if self.project is None:
            raise RuntimeError(
                "no project is open. Call list_projects + open_project first, "
                "or create_project to start a new one."
            )
        return self.project

    def is_in_print_phase(self) -> bool:
        return self.project is not None and self.phase_state.is_active(self.project.id)


STATE = _ServerState()


# --------------------------------------------------------------------- #
# Print API adapter                                                      #
# --------------------------------------------------------------------- #
#
# PrintToolset's tools call methods on a JsApi-shaped object — slice,
# send, set_overrides, etc. The desktop app implements those on JsApi;
# here we implement the same surface backed by `_ServerState`.

class _PrintApiAdapter:
    """Subset of JsApi the print tools need, backed by _ServerState."""

    def __init__(self, state: _ServerState):
        self.state = state

    def _session(self, project_id: str):
        return self.state.phase_state.get(project_id)

    def print_phase_get(self, project_id: str) -> dict:
        s = settings.load()
        session = self._session(project_id)
        return {
            "ok": True,
            "active": session is not None,
            "session": session.to_json() if session else None,
            "printers": [
                {
                    "id": p.get("id"),
                    "name": p.get("name") or p.get("id"),
                    "kind": p.get("kind", "bambu_x1c"),
                    "ip": p.get("ip", ""),
                    "serial": p.get("serial", ""),
                    "has_access_code": bool(p.get("access_code")),
                    "printer_profile": p.get("printer_profile", ""),
                    "process_profile": p.get("process_profile", ""),
                    "filament_profile": p.get("filament_profile", ""),
                }
                for p in s.printers
            ],
            "default_printer_id": s.default_printer_id,
            "presets": [
                {"id": p.id, "label": p.label, "description": p.description}
                for p in PRESETS
            ],
        }

    def print_set_preset(self, project_id: str, preset: str) -> dict:
        session = self._session(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        try:
            lookup_preset(preset)
        except KeyError as e:
            return {"ok": False, "error": str(e)}
        session.preset = preset
        session.last_slice = None
        return {"ok": True, "session": session.to_json()}

    def print_set_overrides(self, project_id: str,
                            overrides: list[dict] | None = None) -> dict:
        from app.printing.slicers import SliceOverride
        session = self._session(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        new: list[SliceOverride] = []
        for ov in (overrides or []):
            if not isinstance(ov, dict):
                continue
            key = str(ov.get("key", "")).strip()
            if not key:
                continue
            new.append(SliceOverride(
                key=key,
                value=str(ov.get("value", "")).strip(),
                note=str(ov.get("note", "") or ""),
            ))
        session.overrides = new
        session.last_slice = None
        return {"ok": True, "session": session.to_json()}

    def print_slice(self, project_id: str) -> dict:
        from app.cad.script_runner import export_models as export_models_script
        from app.printing import build_slicer
        proj = self.state.project
        if proj is None or proj.id != project_id:
            return {"ok": False, "error": "project not open"}
        session = self._session(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}

        cache = proj.path / ".agentcad-cache" / "print"
        cache.mkdir(parents=True, exist_ok=True)
        export_path = cache / "model.3mf"
        visible = [o for o in proj.list_objects() if o.get("visible", True)]
        if not visible:
            return {"ok": False, "error": "no visible objects to print"}
        items = [
            {
                "name": o["name"],
                "script": str(proj.object_source_path(o["name"])),
                "params": str(proj.object_params_path(o["name"])),
            }
            for o in visible
        ]
        try:
            er = export_models_script(
                items, export_path, cwd=proj.path,
                sketches=_sketches_manifest(proj),
                imports=_imports_manifest(proj),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not er.ok:
            return {"ok": False, "error": er.error or "export failed"}
        session.last_export_path = str(export_path)

        s = settings.load()
        slicer = build_slicer("bambu_studio", {
            "cli_path": s.bambu_studio_cli_path,
        })
        printer_hint: dict = {}
        if session.printer_id:
            for p in s.printers:
                if p.get("id") == session.printer_id:
                    printer_hint = {
                        "printer_profile": p.get("printer_profile", ""),
                        "process_profile": p.get("process_profile", ""),
                        "filament_profile": p.get("filament_profile", ""),
                    }
                    break

        result = slicer.auto_orient_and_slice(
            [export_path],
            preset=session.preset,
            overrides=session.overrides,
            out_dir=cache,
            printer_hint=printer_hint or None,
        )
        session.last_slice = result
        return {"ok": result.ok, "session": session.to_json()}

    def print_send(self, project_id: str) -> dict:
        from app.printing import build_printer
        session = self._session(project_id)
        if session is None:
            return {"ok": False, "error": "not in print phase"}
        if session.last_slice is None or not session.last_slice.ok:
            return {"ok": False, "error": "no successful slice — slice first"}
        if not session.printer_id:
            return {"ok": False, "error": "no printer selected"}
        s = settings.load()
        cfg = next((p for p in s.printers if p.get("id") == session.printer_id), None)
        if cfg is None:
            return {"ok": False, "error": f"printer {session.printer_id!r} not configured"}
        try:
            printer = build_printer(cfg.get("kind", "bambu_x1c"), cfg)
        except Exception as e:
            return {"ok": False, "error": f"could not init printer: {e}"}
        ok, msg = printer.send_print(Path(session.last_slice.sliced_path))
        session.last_send_ok = ok
        session.last_send_message = msg
        return {"ok": ok, "message": msg, "session": session.to_json()}


def _sketches_manifest(project: Project) -> list[dict]:
    return [
        {
            "name": s["name"],
            "script": str(project.sketch_source_path(s["name"])),
            "params": str(project.sketch_params_path(s["name"])),
        }
        for s in project.list_sketches()
    ]


def _imports_manifest(project: Project) -> list[dict]:
    out: list[dict] = []
    for i in project.list_imports():
        path = project.import_source_path(i["name"])
        if path is None:
            continue
        out.append({"name": i["name"], "path": str(path)})
    return out


# --------------------------------------------------------------------- #
# SDK schema → JSON Schema conversion                                    #
# --------------------------------------------------------------------- #


_TYPE_MAP: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _to_json_schema(sdk_schema: Any) -> dict:
    """Convert claude_agent_sdk's `{name: type}` shorthand to JSON Schema.

    All fields are marked optional — that's how the SDK treats them
    (handlers do `args.get(...)` rather than asserting presence)."""
    if not isinstance(sdk_schema, dict):
        return {"type": "object", "properties": {}}
    props: dict[str, dict] = {}
    for key, ty in sdk_schema.items():
        json_ty = _TYPE_MAP.get(ty, "string")
        props[key] = {"type": json_ty}
    return {"type": "object", "properties": props, "additionalProperties": True}


def _convert_handler_result(result: Any) -> list:
    """Translate a CadToolset/PrintToolset handler result to MCP content.

    Inputs:
        {"content": [{"type":"text","text":"…"}, {"type":"image","data":"…","mimeType":"…"}], "is_error": bool}
    Output:
        list of mcp.types.{TextContent, ImageContent}
    The is_error flag is hoisted onto the *first* TextContent's metadata
    by the MCP server framework when we raise; here we just pack up the
    content blocks. Errors come through as text starting with "[error]".
    """
    if not isinstance(result, dict):
        return [TextContent(type="text", text=str(result))]
    content = result.get("content") or []
    out: list = []
    is_error = bool(result.get("is_error"))
    for c in content:
        if isinstance(c, dict):
            kind = c.get("type")
            if kind == "text":
                text = c.get("text", "")
                if is_error and not text.startswith("[error]"):
                    text = f"[error] {text}"
                out.append(TextContent(type="text", text=text))
            elif kind == "image":
                data = c.get("data") or ""
                mime = c.get("mimeType") or "image/png"
                out.append(ImageContent(type="image", data=data, mimeType=mime))
            else:
                # Unknown content type — fall back to JSON dump.
                out.append(TextContent(type="text", text=json.dumps(c)))
    if not out:
        out.append(TextContent(type="text", text="(no content)"))
    return out


# --------------------------------------------------------------------- #
# Project-management tools (added on top of the existing CAD/print sets) #
# --------------------------------------------------------------------- #


async def _tool_list_projects(args: dict) -> dict:
    s = settings.load()
    parent = args.get("parent_dir") or s.default_project_dir
    p = Path(parent).expanduser()
    rows = list_recent(p)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"parent_dir": str(p), "projects": rows}, indent=2),
            }
        ]
    }


async def _tool_open_project(args: dict) -> dict:
    path = (args.get("path") or "").strip()
    if not path:
        return {"content": [{"type": "text", "text": "[error] 'path' is required"}],
                "is_error": True}
    try:
        project = Project.open(Path(path).expanduser())
    except Exception as e:
        return {"content": [{"type": "text", "text": f"[error] could not open: {e}"}],
                "is_error": True}
    STATE.set_project(project)
    return {"content": [{
        "type": "text",
        "text": f"opened {project.title!r} at {project.path}\n"
                f"{json.dumps(project.to_json(), indent=2, default=str)}",
    }]}


async def _tool_create_project(args: dict) -> dict:
    name = (args.get("name") or "").strip()
    if not name:
        return {"content": [{"type": "text", "text": "[error] 'name' is required"}],
                "is_error": True}
    s = settings.load()
    parent = Path(args.get("parent_dir") or s.default_project_dir).expanduser()
    parent.mkdir(parents=True, exist_ok=True)
    try:
        project = Project.create_named(parent, name)
    except (FileExistsError, ValueError) as e:
        return {"content": [{"type": "text", "text": f"[error] {e}"}], "is_error": True}
    STATE.set_project(project)
    return {"content": [{
        "type": "text",
        "text": f"created and opened {project.title!r} at {project.path}",
    }]}


async def _tool_current_project(args: dict) -> dict:  # noqa: ARG001
    if STATE.project is None:
        return {"content": [{"type": "text", "text": "(no project open)"}]}
    return {"content": [{
        "type": "text",
        "text": json.dumps(STATE.project.to_json(), indent=2, default=str),
    }]}


async def _tool_enter_print_phase(args: dict) -> dict:
    proj = STATE.project
    if proj is None:
        return {"content": [{"type": "text", "text": "[error] no project open"}],
                "is_error": True}
    s = settings.load()
    if not s.printers:
        return {"content": [{
            "type": "text",
            "text": "[error] no 3D printer configured. Add one in the desktop "
                    "app's Settings or write to ~/.agent-cad/settings.json.",
        }], "is_error": True}
    session = STATE.phase_state.enter(proj.id)
    preset = args.get("preset")
    if preset:
        try:
            lookup_preset(preset)
            session.preset = preset
        except KeyError as e:
            return {"content": [{"type": "text", "text": f"[error] {e}"}], "is_error": True}
    if not session.printer_id:
        session.printer_id = s.default_printer_id or s.printers[0]["id"]
    return {"content": [{
        "type": "text",
        "text": f"entered print phase. preset={session.preset}, "
                f"printer={session.printer_id}. CAD-editing tools are "
                f"hidden until leave_print_phase is called.",
    }]}


async def _tool_leave_print_phase(args: dict) -> dict:  # noqa: ARG001
    proj = STATE.project
    if proj is None:
        return {"content": [{"type": "text", "text": "(no project open)"}]}
    STATE.phase_state.leave(proj.id)
    return {"content": [{"type": "text", "text": "left print phase."}]}


async def _tool_printer_camera_snapshot(args: dict) -> dict:
    """Grab a single frame from the configured printer's camera.

    Returns the PNG inline as MCP image content so the agent can see it
    directly. Available regardless of phase — peeking at the camera
    doesn't require entering print phase, and being able to ask "is it
    actually printing?" outside of print phase is the common case.
    """
    printer_id = (args.get("printer_id") or "").strip() or None
    # Run the blocking RTSPS read off the event loop so we don't stall
    # the MCP transport while FFmpeg negotiates the stream.
    res = await asyncio.to_thread(grab_camera_frame, printer_id=printer_id)
    if not res.ok:
        return {
            "content": [{"type": "text", "text": f"[error] {res.error}"}],
            "is_error": True,
        }
    label = res.printer_name or res.printer_id or "printer"
    return {
        "content": [
            {"type": "text", "text": f"live frame from {label}"},
            {
                "type": "image",
                "data": base64.b64encode(res.png_bytes or b"").decode("ascii"),
                "mimeType": "image/png",
            },
        ],
    }


_PROJECT_MGMT_TOOLS = [
    (
        "list_projects",
        "List Agent CAD projects under a directory (defaults to the user's "
        "configured project dir at ~/.agent-cad/projects/, overridable via "
        "settings.default_project_dir or this tool's `parent_dir` arg). Returns "
        "each project's path, title, head sha, last modified time. Pair with "
        "open_project to start working on one.",
        {"parent_dir": str},
        _tool_list_projects,
    ),
    (
        "open_project",
        "Open the project at `path` and make it the current project. All other "
        "tools (run_model, snapshot, measure, …) operate on whatever project is "
        "currently open. Replaces any previously-open project.",
        {"path": str},
        _tool_open_project,
    ),
    (
        "create_project",
        "Create a new Agent CAD project under `parent_dir` (defaults to the "
        "configured project dir). Initialises git, seeds a `main` object, and "
        "opens the project so subsequent tools target it. Returns the new "
        "project's absolute path.",
        {"name": str, "parent_dir": str},
        _tool_create_project,
    ),
    (
        "current_project",
        "Return the full state of whichever project is currently open: "
        "objects, sketches, imports, active artifact, head commit, etc. "
        "Empty result when nothing is open.",
        {},
        _tool_current_project,
    ),
    (
        "enter_print_phase",
        "Switch the current project into the print phase. While in this phase, "
        "CAD-editing tools (run_model, snapshot, …) are disabled and the "
        "print-phase tools (slice_for_print, set_print_preset, …) become the "
        "active surface — same gating the desktop UI applies. Optional `preset` "
        "kicks off with one of strong/standard/fine; default is standard.",
        {"preset": str},
        _tool_enter_print_phase,
    ),
    (
        "leave_print_phase",
        "Return to plain CAD mode. The CAD-editing tools become available again "
        "and the print tools refuse to run.",
        {},
        _tool_leave_print_phase,
    ),
    (
        "printer_camera_snapshot",
        "Grab one live PNG frame from the configured printer's chamber camera "
        "and return it inline. Use this to confirm what the printer is actually "
        "doing — \"is it still printing?\", \"did the part come loose?\", etc. "
        "Available regardless of phase. Optional `printer_id` selects a "
        "specific printer when multiple are configured (defaults to "
        "`default_printer_id` from settings).",
        {"printer_id": str},
        _tool_printer_camera_snapshot,
    ),
]


# --------------------------------------------------------------------- #
# Wire-up                                                                #
# --------------------------------------------------------------------- #


def _build_cad_tool_index() -> dict[str, tuple[McpTool, Any]]:
    """Build a {tool_name: (mcp_tool_metadata, handler)} dict for CAD
    tools. The handler is async and takes a dict, same shape as the
    SDK's tool surface — we just translate the result on the way out."""
    assert STATE.cad_toolset is not None
    out: dict[str, tuple[McpTool, Any]] = {}
    for sdk_tool in build_cad_tools(STATE.cad_toolset):
        meta = McpTool(
            name=sdk_tool.name,
            description=sdk_tool.description,
            inputSchema=_to_json_schema(sdk_tool.input_schema),
        )
        out[sdk_tool.name] = (meta, sdk_tool.handler)
    return out


def _build_print_tool_index() -> dict[str, tuple[McpTool, Any]]:
    assert STATE.print_toolset is not None
    out: dict[str, tuple[McpTool, Any]] = {}
    for sdk_tool in build_print_tools(STATE.print_toolset):
        meta = McpTool(
            name=sdk_tool.name,
            description=sdk_tool.description,
            inputSchema=_to_json_schema(sdk_tool.input_schema),
        )
        out[sdk_tool.name] = (meta, sdk_tool.handler)
    return out


def _build_project_mgmt_tool_index() -> dict[str, tuple[McpTool, Any]]:
    out: dict[str, tuple[McpTool, Any]] = {}
    for name, desc, schema, handler in _PROJECT_MGMT_TOOLS:
        meta = McpTool(
            name=name,
            description=desc,
            inputSchema=_to_json_schema(schema),
        )
        out[name] = (meta, handler)
    return out


def _ensure_toolsets() -> None:
    """Tools close over `toolset.project`; if no project is open we still
    need a toolset object so list_tools can build the metadata. We give
    it a placeholder Project that lives in a temp dir until something
    real opens."""
    if STATE.cad_toolset is None or STATE.print_toolset is None:
        # Use the first available recent project as a placeholder so the
        # toolsets have a real Project to point at. If nothing exists,
        # create one in a scratch dir; it'll be replaced as soon as the
        # agent calls open_project / create_project.
        s = settings.load()
        candidates = list_recent(Path(s.default_project_dir).expanduser())
        if candidates:
            placeholder = Project.open(Path(candidates[0]["path"]))
        else:
            scratch = Path(s.default_project_dir).expanduser() / "_scratch"
            scratch.parent.mkdir(parents=True, exist_ok=True)
            try:
                placeholder = Project.create_named(scratch.parent, scratch.name)
            except FileExistsError:
                placeholder = Project.open(scratch)
        STATE.set_project(placeholder)


async def _serve(initial_project: Path | None) -> None:
    if initial_project is not None:
        STATE.set_project(Project.open(initial_project))
    _ensure_toolsets()

    cad_index = _build_cad_tool_index()
    print_index = _build_print_tool_index()
    mgmt_index = _build_project_mgmt_tool_index()

    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[McpTool]:
        # The CAD vs. print tool surfaces are mutually exclusive — same
        # rule the desktop app's chat agent enforces. Project-mgmt tools
        # are always available so the agent can switch projects / enter
        # the phase regardless of which surface is currently active.
        if STATE.is_in_print_phase():
            primary = print_index
        else:
            primary = cad_index
        out: list[McpTool] = []
        for meta, _h in mgmt_index.values():
            out.append(meta)
        for meta, _h in primary.values():
            out.append(meta)
        return out

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None) -> list:
        args = arguments or {}
        for index in (mgmt_index, cad_index, print_index):
            if name in index:
                _meta, handler = index[name]
                try:
                    result = await handler(args)
                except Exception as e:
                    return [TextContent(
                        type="text",
                        text=f"[error] {name} raised: {type(e).__name__}: {e}",
                    )]
                return _convert_handler_result(result)
        return [TextContent(type="text", text=f"[error] unknown tool {name!r}")]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent-cad-mcp")
    parser.add_argument(
        "--project",
        help="open this project at startup (otherwise call open_project later)",
    )
    args = parser.parse_args()
    initial = Path(args.project).expanduser() if args.project else None
    try:
        asyncio.run(_serve(initial))
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
