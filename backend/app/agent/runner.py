"""Run the Claude Agent for one chat turn against a project.

The agent sees the project directory as its working directory. Each
project has one or more *objects* under `objects/`, and exactly one is
*active* at a time. Read/Edit/Write the active object's script; CAD tools
operate on the active object automatically.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
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

from .. import browser_session, permissions, settings
from ..cad.project import Project
from ..cad.script_runner import RunResult
from ..events import bus
from .tools import (
    ALL_TOOL_NAMES,
    BUILTIN_TOOLS,
    PRINT_TOOL_NAMES,
    CadToolset,
    PrintToolset,
    build_cad_server,
    build_print_server,
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

# `@playwright/mcp` uses the `with { type: "json" }` import attribute
# syntax (added in Node 20.10) and bundles a yauzl that needs the
# require/import interop fixes from that release line. Older Node
# crashes the MCP subprocess silently on first import, so no tools
# register and the agent looks stuck. We pre-flight the version below.
PW_MIN_NODE: tuple[int, int, int] = (20, 10, 0)


def _node_version() -> tuple[int, int, int] | None:
    """Returns the local Node version as (major, minor, patch), or None
    if Node isn't on PATH or its `--version` output can't be parsed."""
    node = shutil.which("node")
    if not node:
        return None
    try:
        proc = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    out = (proc.stdout or "").strip()
    if out.startswith("v"):
        out = out[1:]
    parts = out.split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


PLAYWRIGHT_PROMPT_BLOCK = """
PLAYWRIGHT BROWSER (experimental, enabled):
The user has explicitly enabled a real Chromium browser for you to
drive. USE IT — it is not a last-resort tool. The user can SEE the
browser in a floating window in the app, so they're tracking what
you're doing and they expect to see it work for the use cases below.

When to reach for Playwright instead of (or after) WebSearch / WebFetch:
- ANY MODEL MARKETPLACE: Thingiverse, Printables, GrabCAD, MakerWorld,
  Cults3D, Yeggi, Pinshape, etc. These sites are JS-heavy, gate
  downloads behind login or click-throughs, and serve files that
  WebFetch can't follow. Skip web search entirely for these — go
  STRAIGHT to browser_navigate on the site's search URL.
- DOWNLOADING any model file from a site (vs. an API like Sketchfab's).
  WebFetch CANNOT trigger a real download click; Playwright can.
  Workflow: navigate to the site → search for the part → click into
  the listing → click the download button → the file lands in the
  browser's downloads dir → use Read or filesystem ops to pick it up
  and copy into imports/.
- Pages where WebFetch returns "blocked", "JS required", a login wall,
  or a near-empty body. Don't keep trying WebFetch — switch to
  Playwright on the same URL.
- Manufacturer datasheet portals that need a click-through ("I agree"
  banners, region selectors, etc.).
- JS-heavy product configurators (PCB connectors, screw spec lookups
  with dropdown filters, etc.).
- ANY time you've already failed twice on a page with WebFetch /
  WebSearch and you still need the data — escalate to Playwright
  rather than guess. The user said it explicitly: don't fabricate
  dimensions, look them up, and Playwright is your strongest tool
  for that.

Tool surface (all under mcp__playwright__):
- browser_navigate(url)            open or reuse a tab
- browser_snapshot()               accessibility tree + text → use
                                    this BEFORE clicking; it's cheaper
                                    than a screenshot and gives you
                                    selectors directly
- browser_take_screenshot()        actual rendered image; use after a
                                    navigate for visual confirmation
- browser_click({element, ref})    click via aria/role + index
- browser_type({element, ref, text, submit?}) fill an input
- browser_press_key(key)           Enter / Tab / arrow keys
- browser_wait_for({text|time|...}) wait for content to appear
- browser_evaluate(function)       run JS in the page when nothing
                                    else fits
- browser_close()                  call this at the END so the
                                    embedded window doesn't linger
                                    on stale state

Permissions:
- The user may have "ask before each browser action" enabled. When on,
  every browser_* call pauses and asks for approval in the chat. If
  you get denied, don't retry the same tool — try a different angle
  (search the open page differently, navigate elsewhere, ask the
  user, etc.).

User-assisted interaction (bot gates / CAPTCHAs / logins):
- The embedded browser window has a small "interact" toggle. When the
  user flips it on, their clicks + keystrokes go straight into the page
  (alongside yours, on the same Chromium). That's how they help past
  CAPTCHAs, login walls, age gates, geo-blockers, etc.
- Bot gates you MUST stop on, on first sight, no retries:
    * Cloudflare "Checking your browser" / Turnstile interstitial
    * "Verify you are human" / "Are you a robot?" / reCAPTCHA / hCaptcha
    * "Press and hold" sliders / image-puzzle challenges (e.g. PerimeterX,
      DataDome, Akamai Bot Manager)
    * Sign-in walls, age gates, region selectors that won't dismiss
    * Pages that snap straight to "Access Denied" / 403 / "Suspicious
      activity detected"
- The MOMENT a snapshot or screenshot shows any of the above, hand off:
    1. STOP. Do not click, type, retry, refresh, or navigate elsewhere.
       Re-clicking the same element burns trust with the bot detector
       and refreshing usually wipes the user's in-progress solve.
    2. Ask the user — via AskUserQuestion — to flip the embedded
       browser's "interact" toggle and clear the gate themselves.
       Briefly describe what you see ("Cloudflare challenge on
       printables.com — please solve it in the embedded browser, then
       reply 'done' here").
    3. WAIT for their reply before any further browser_* call. Do not
       poll the page in the meantime.
    4. After they confirm, take ONE fresh browser_snapshot to verify
       the page advanced. If it did, continue. If the gate's still
       there, ask again — don't retry on your own.
- Login walls follow the same rule: stop, ask, wait. The user can
  type credentials directly in the embedded browser when "interact" is
  on; you should never try to autofill credentials yourself.
"""


PRINT_PHASE_PROMPT_TEMPLATE = """
PRINT PHASE (active for this turn):
The user has switched the project into the *print phase*. The viewer
area on screen is now showing print options instead of the CAD scene.
You are still the agent — the chat panel is right next to the print
panel, the user can talk to you mid-flow, and they expect you to drive
the slicer.

Current print state:
- printer:        {printer_label}
- preset:         {preset}
- overrides:      {overrides_summary}
- last slice:     {last_slice_summary}

Your job in this turn:
  1. If the model isn't sliced yet (no last slice or it errored), call
     mcp__cad__slice_for_print. Bambu Studio's CLI will auto-orient the
     part as part of that pass.
  2. Look at the slice estimate. If the user explicitly named a goal
     ("strong" / "fine" / "fast" / a target time), set the preset with
     mcp__cad__set_print_preset before slicing.
  3. Apply overrides only when the geometry warrants it — eg. tall thin
     features want supports, hollow internal volumes might want a higher
     wall count, a mechanical clip might want stronger infill than the
     preset's default. Use mcp__cad__add_print_override / clear_print_overrides.
     Each override has a short `note` so the user understands WHY you set it.
  4. When the user is happy with the preset + overrides + slice, call
     mcp__cad__send_to_printer to upload + start the print over LAN.
     Confirm before sending — printing wastes filament and time, so don't
     auto-send unless the user told you to.
  5. AFTER sending, take a `mcp__cad__printer_snapshot` to verify the
     print actually started cleanly. The first 30s catches most failures
     (no filament loaded, bad first layer, plate-detection wrong) and
     it's much cheaper to abort then than 8 hours in.

Live monitoring tools:
  - mcp__cad__print_status pulls fresh MQTT state — gives you the
    printer's gcode_state (RUNNING / PAUSE / FINISH / FAILED), progress %,
    layer N/M, time remaining, nozzle/bed temps. Use it to answer "is
    it done yet?" / "how's it going?" without spamming the camera.
  - mcp__cad__printer_snapshot grabs one JPEG frame from the chamber
    camera and returns it for you to actually SEE (you're multimodal).
    Use it the moment the user expresses any concern about visual print
    quality — stringing, layer shifts, parts pulling off the bed,
    nozzle clogs, weird colour. Camera is wide-FOV and lit greenish by
    the chamber LED; the colour cast is normal.

You DO NOT have CAD tools in the print phase — no Edit, run_model,
snapshot, etc. If the user wants to change the geometry they have to
leave the print phase first ("back to CAD") and you'll get the editing
toolset back. If they ask you to fix something model-side from inside
the print phase, tell them to click the back arrow first.
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

A SINGLE EXTRUDED PROFILE IS RARELY THE WHOLE PART:
A real designed object almost always wants more than one sketch. If
your only sketch is the side silhouette and your object script is one
.extrude() call, you've made an extruded drawing, not a designed part.
Plan for AT LEAST 2-3 sketches per non-trivial object, on different
planes, used at different stages of the build:

  - Main profile sketch on its natural plane (the silhouette).
  - One or more SECONDARY sketches for cutouts: cable channels,
    ports, screw bosses, finger reliefs, weight-reduction pockets,
    drainage slots. These MUST be on a plane PERPENDICULAR to the
    main extrusion direction — see the next section for the rule.
  - Sketches for raised features: bosses, locating ribs, snap-fit
    tabs, brand text, alignment dots. Place these on the face they
    belong on via cq.Plane / .workplane(offset=...) or by selecting a
    face after the main extrude (`wp.faces(">Z").workplane()`).

THE PERPENDICULAR-PLANE RULE (read this carefully — it's the bug
that keeps producing "extruded drawing" parts):

If you draw your main silhouette on XY and extrude along +Z, then a
cutout sketch ALSO on XY only adds another shape to the same 2D
profile — extruding it just gives you another vertical column, not a
hole or channel through the part. To cut a cable slot or a port THROUGH
the part, the cutout sketch has to live on a plane whose normal points
ALONG one of the part's IN-PLANE axes — i.e. perpendicular to the
extrusion direction.

Concrete rule:
  - Main extrusion along +Z (sketch on XY)  →  cutouts on XZ or YZ
    (or an offset plane like `cq.Plane((0,0,h/2), (1,0,0), (0,0,1))`
    sitting mid-height through the body).
  - Main extrusion along +Y (sketch on XZ)  →  cutouts on XY or YZ.
  - Main extrusion along +X (sketch on YZ)  →  cutouts on XY or XZ.

Then in the object script:
  cutter = sketches["cable-slot"].extrude(<long enough to clear the body>)
  model  = body.cut(cutter)

ANTI-PATTERN — every sketch on the same plane: if every sketch in the
project sits on XY (or whatever the main plane is), you're stacking
2D drawings, not designing in 3D. The result will be a single
extruded silhouette with extra outline detail and nothing going
ACROSS it. Stop and rethink which plane each sketch belongs on
BEFORE writing the object script.

Worked example — a phone stand with a side silhouette + cable slot +
back-relief pocket:
  sketches/profile.py        plane = "XZ"                    # the L-shape side view
  sketches/cable-slot.py     plane = "XY"                    # rounded rect, the slot
                                                             # cuts ALONG +Y through the base
  sketches/back-relief.py    plane = ("YZ", offset = depth)  # pocket on the back surface
Object script:
  body = sketches["profile"].extrude(width)                  # along +Y
  body = body.cut(sketches["cable-slot"].extrude(width))     # cuts a channel through the base
  body = body.cut(sketches["back-relief"].extrude(-3))       # depression on back
  model = body.edges("|Z").fillet(2.0)                       # finishing

Build the part in PASSES. After each pass, run_model + snapshot, then
decide what's missing:
  Pass 1 — rough mass: extrude the silhouette, basic boolean unions
            for primary features.
  Pass 2 — cutouts: cut the cable slots, ports, holes, reliefs from
            secondary sketches.
  Pass 3 — finishing: fillets on contact / bottom / human-touched
            edges, chamfers on lead-ins and sharp top corners. This
            is where a part stops looking like a CAD primitive and
            starts looking polished.

FINISHING-PASS CHECKLIST (don't ship without thinking about these):
  - Fillets on the bottom edges that touch a surface (anti-scratch).
  - Fillets / chamfers on edges the user grips or their phone rests
    against — sharp 90° corners look amateur and scratch what they
    touch.
  - Chamfers on lead-in edges of any slot the user inserts something
    into (cables, phones, parts).
  - Wall thickness ≥ 2 mm anywhere it's loaded, unless the user said
    otherwise. Check internal pockets too.
  - Symmetry / mirroring where the design is supposed to be symmetric.
  - For 3D printing: avoid thin overhangs; consider draft on tall
    vertical walls if the user is printing FDM.
  - Did you actually use the cable / cord slot the user mentioned?
    Don't drop requested features in pass 2 because pass 1 looked
    "good enough."

STRUCTURAL + STABILITY VALIDATION (do this after fillets, before
commit_turn):
  - Call check_validity. If it reports anything other than a clean
    valid solid (self-intersections, degenerate faces, non-manifold
    edges), STOP and fix it — a "valid" CAD-runnable script can still
    produce broken geometry. A part that doesn't pass validity won't
    print and won't boolean correctly downstream.
  - Call mass_properties to get center of mass + bbox. For any part
    that RESTS ON a surface (stands, brackets, racks, hooks, lamps —
    basically anything with a bottom), verify the COM's (x, y)
    projects INSIDE the footprint of the bottom face. If it doesn't,
    the part will tip the instant the user puts it down — widen the
    base, move the heavy mass over the support, or add a counterweight
    feature. Be especially cautious with anything that holds weight
    above a narrow footprint (phone stands at steep angles,
    cantilever shelves).
  - For load-bearing parts: check the cross-section at the load path.
    A 2 mm wall holding a 200 g phone at the top of a tall stand will
    flex visibly. If the user expects rigidity, thicken it or add a
    rib (which is its own sketch + extrude → another reason there
    should be more than one sketch in the project).
  - For mating / inserted parts (a screw boss, a peg in a hole,
    something that slots into a port): use measure / query_faces /
    distance_between to confirm the clearance you intended is what
    actually got built. The wrong sketch dimension is the #1 way a
    print comes off the bed not fitting.

If after run_model + snapshot the part still looks like a flat
extrusion of one profile, you're not done — go back and add the
secondary sketches and the finishing fillets. The user has called
this out specifically: extruded silhouettes feel unfinished.

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
  bug. If WebFetch comes back empty / blocked / JS-required, and
  Playwright is enabled, switch to mcp__playwright__browser_navigate on
  the same URL — DON'T just give up and retry with Search.
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


def _build_system_prompt(project: Project, *, playwright_active: bool,
                          print_phase: dict | None = None) -> str:
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
    # Only advertise Playwright in the prompt when it actually registered.
    # Otherwise the agent thinks browser tools are available and gets stuck
    # searching for them.
    if playwright_active:
        body += PLAYWRIGHT_PROMPT_BLOCK
    if print_phase and print_phase.get("active"):
        printer = print_phase.get("printer") or {}
        printer_label = (
            f"{printer.get('name', '?')} ({printer.get('kind', '?')})"
            if printer else "no printer selected"
        )
        overrides = print_phase.get("overrides") or []
        if overrides:
            overrides_summary = ", ".join(
                f"{o['key']}={o['value']}" for o in overrides
            )
        else:
            overrides_summary = "(none — preset defaults)"
        last = print_phase.get("last_slice")
        if last and last.get("ok"):
            mins = last.get("estimated_minutes")
            gms = last.get("estimated_filament_g")
            bits = []
            if mins is not None:
                h, m = int(mins // 60), int(mins % 60)
                bits.append(f"~{h}h{m:02d}m" if h else f"~{m}m")
            if gms is not None:
                bits.append(f"~{gms:.0f}g")
            last_summary = ", ".join(bits) if bits else "ok"
        elif last:
            last_summary = f"FAILED: {last.get('error') or 'unknown error'}"
        else:
            last_summary = "(not sliced yet)"
        body += PRINT_PHASE_PROMPT_TEMPLATE.format(
            printer_label=printer_label,
            preset=print_phase.get("preset", "standard"),
            overrides_summary=overrides_summary,
            last_slice_summary=last_summary,
        )
    return body


def _make_permission_callback(project: Project, msg_id: str, *,
                              require_permission: bool):
    """Build a can_use_tool callback that:
      1. Lazy-starts our embedded Chromium the first time the agent
         actually fires a Playwright tool (so idle Playwright-enabled
         turns don't incur the Chromium spawn cost).
      2. Auto-allows our own CAD tools + standard editor tools.
      3. If require_permission, routes everything else through the
         chat permission card; otherwise auto-allows.
    """

    async def callback(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ):
        # Always-allow surface: our own CAD tools and the SDK's safe
        # built-ins. Playwright is the surface that may need a prompt.
        if (
            tool_name.startswith("mcp__cad__")
            or tool_name in ("Read", "Write", "Edit", "Glob", "Grep",
                             "WebSearch", "WebFetch", "TodoWrite",
                             "AskUserQuestion")
        ):
            return PermissionResultAllow()

        if not require_permission:
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
    extra_context: dict | None = None,
    print_api: Any | None = None,
) -> None:
    """Fire-and-forget agent invocation. Progress streams via bus.emit.

    `print_api` (when provided) is a JsApi-like object the print-phase
    tools call back into for slice / send / state mutations. We pass it
    in rather than importing JsApi here to avoid a runner→api circular
    dependency.
    """
    msg_id = msg_id or f"msg_{uuid.uuid4().hex[:8]}"
    extra_context = extra_context or {}
    print_phase = (extra_context.get("print_phase") or {}) if extra_context else {}
    if print_phase.get("active") and print_api is not None:
        # In the print phase the toolset object the runner hands the
        # tools is a PrintToolset — it doesn't run model.py scripts, so
        # tools.py's CAD-specific surface stays out of scope.
        toolset = PrintToolset(project, print_api)
    else:
        toolset = CadToolset(project, on_run)

    def _worker():
        try:
            asyncio.run(_run(project, toolset, prompt, attachments, msg_id, extra_context))
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


async def _run(project: Project, toolset, prompt: str,
               attachments: list[dict] | None, msg_id: str,
               extra_context: dict) -> None:
    user_settings = settings.load()
    print_phase = (extra_context.get("print_phase") or {}) if extra_context else {}

    mcp_servers: dict[str, Any] = {}
    if isinstance(toolset, PrintToolset):
        mcp_servers["cad"] = build_print_server(toolset)
        # Print phase has no CAD editing — only print tools + safe builtins.
        allowed_tools = list(BUILTIN_TOOLS) + list(PRINT_TOOL_NAMES)
    else:
        mcp_servers["cad"] = build_cad_server(toolset)
        allowed_tools = list(ALL_TOOL_NAMES)
    cdp_http: str | None = None
    playwright_active = user_settings.playwright_enabled
    if playwright_active:
        node_ver = _node_version()
        if node_ver is None:
            bus.emit("chat_event", {
                "doc_id": project.id, "msg_id": msg_id,
                "kind": "text",
                "text": (
                    "[playwright disabled] Node.js wasn't found on PATH, so "
                    "the Playwright MCP server can't start. Install Node "
                    f"{PW_MIN_NODE[0]}.{PW_MIN_NODE[1]} or newer "
                    "(https://nodejs.org), then restart the app."
                ),
            })
            playwright_active = False
        elif node_ver < PW_MIN_NODE:
            v = ".".join(str(x) for x in node_ver)
            req = ".".join(str(x) for x in PW_MIN_NODE)
            bus.emit("chat_event", {
                "doc_id": project.id, "msg_id": msg_id,
                "kind": "text",
                "text": (
                    f"[playwright disabled] Node.js {v} is too old — "
                    f"`@playwright/mcp` needs Node {req} or newer "
                    "(it uses the `with {{ type: 'json' }}` import-attribute "
                    "syntax, added in 20.10). Upgrade Node "
                    "(https://nodejs.org), then restart the app."
                ),
            })
            playwright_active = False

    if playwright_active:
        # Eagerly start Chromium when Playwright is enabled. The original
        # plan was to defer this to the can_use_tool callback ("only spawn
        # when the agent first calls a browser tool"), but with
        # permission_mode="bypassPermissions" the CLI never fires the
        # callback at all — so MCP would try to connect to the CDP port
        # before Chromium was up and get ECONNREFUSED. The panel still
        # only auto-shows when the agent actually navigates somewhere
        # (the screencast loop suppresses about:blank), so the user
        # doesn't see anything until there's something worth showing.
        if not browser_session.session.is_running:
            await asyncio.to_thread(browser_session.session.ensure_started)
        cdp_http = browser_session.session.cdp_http_endpoint

        pw_args = ["-y", "@playwright/mcp@latest"]
        if cdp_http:
            pw_args.extend(["--cdp-endpoint", cdp_http])
        else:
            # Couldn't find / start Chromium — let MCP launch its own;
            # we just won't get the screencast.
            bus.emit("chat_event", {
                "doc_id": project.id, "msg_id": msg_id,
                "kind": "text",
                "text": (
                    "[browser session note] couldn't find Chromium for the "
                    "embedded preview, so playwright-mcp will launch its own "
                    "browser without screencast. Install Chrome / Edge or run "
                    "`npx playwright install chromium` to enable the live view."
                ),
            })
        # Windows: `npx` exists as both a shell script and a `.cmd` batch
        # file. Subprocess spawn (used by the MCP stdio transport) can
        # only execute the latter without a shell, so it silently fails
        # with bare "npx" and the playwright tools never register.
        # Prefer the absolute path of npx.cmd if we can find one.
        npx_cmd = shutil.which("npx.cmd") if os.name == "nt" else None
        mcp_servers["playwright"] = {
            "type": "stdio",
            "command": npx_cmd or "npx",
            "args": pw_args,
        }
        allowed_tools = list(allowed_tools) + PLAYWRIGHT_TOOL_NAMES

    # Install a can_use_tool callback whenever Playwright is enabled —
    # we need it for the lazy-Chromium hook even when permission
    # prompts are off. The callback itself decides whether to ask the
    # user based on require_permission. The SDK requires streaming-mode
    # input when can_use_tool is set, so we always wrap the prompt below
    # in that case.
    can_use_tool = None
    if playwright_active:
        can_use_tool = _make_permission_callback(
            project, msg_id,
            require_permission=user_settings.playwright_require_permission,
        )

    options = ClaudeAgentOptions(
        cwd=str(project.path),
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        system_prompt=_build_system_prompt(
            project,
            playwright_active=playwright_active,
            print_phase=print_phase,
        ),
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
