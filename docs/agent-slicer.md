# Splitting Agent CAD into Agent CAD + Agent Slicer

> **Status:** Design proposal for v0.2.0. Not yet implemented.
> Confirm the architecture in this doc before any code lands.

## Motivation

v0.1.0 ships modeling and slicing in one process. The "print phase" is a
modal overlay that hides the CAD viewer and shows print options instead.
That worked as a starting point, but the two domains pull the UI in
opposite directions:

| | Agent CAD | Agent Slicer |
|---|---|---|
| **Object model** | Parametric scripts; one active object | Multi-model 3MF; many copies of many parts |
| **Viewer** | One scene; pick face/edge/vertex | Many plates; pick + drag whole models |
| **Working state** | Lives in CADQuery, scripts on disk | Lives inside a 3MF (placement, orientation) |
| **Agent's job** | Write code that builds geometry | Arrange, orient, slice, send to printer |
| **User's mental model** | "Design this part" | "Lay out this print job" |

Bolting both into one UI keeps each compromised. v0.2.0 splits them.

## Goals

1. **Two separate apps.** `agent-cad` and `agent-slicer` are independent
   pywebview windows with their own backends and frontends.
2. **One handoff.** Agent CAD has a "Slice this" action that exports the
   active object (or a multi-object selection) and launches Agent Slicer
   with the resulting file path.
3. **3MF-native slicer.** The slicer's working document is a 3MF file.
   Open / save / "save as" all operate on 3MFs. Plate state lives in
   `Metadata/model_settings.config` per the Bambu Studio convention.
4. **Visual plate UI.** Multi-plate tabs (Plate 1, Plate 2, …), a 3D
   bed scene per plate, drag-to-move within a plate, right-click /
   keyboard / agent-driven move-between-plates.
5. **Same agent UX.** Chat panel, drawing dialog, snapshot tool, todo
   panel — all preserved. The agent's tool surface changes.
6. **No regression for v0.1.0 users.** Existing projects keep working
   in Agent CAD; the slicer is additive. The legacy print phase stays
   in Agent CAD until Agent Slicer reaches feature parity, then goes.

## Non-goals (for v0.2.0)

- Custom slicer engine. We continue to call out to Bambu Studio CLI
  for actual slicing; eventually we'll add PrusaSlicer / OrcaSlicer
  CLI variants, but that's later.
- Native printer support beyond Bambu X1C LAN mode.
- gcode preview rendering. The slicer shows the 3MF model, the slice
  estimate, and (post-slice) the printer's gcode time estimate — but
  not a visual layer-by-layer preview. That's its own milestone.

## Repo layout

Single repo, two apps. Sister-directory layout (lighter than a
full monorepo migration):

```
cc-cad/                           # repo root (consider rename to cc-agent-tools later)
├── apps/
│   ├── cad/                      # what is currently `backend/` + `frontend/`
│   │   ├── backend/
│   │   │   └── app/              # CADQuery exec, project model, sketches
│   │   ├── frontend/             # CAD-specific UI (Tweaks, Timeline, viewer)
│   │   ├── run.py
│   │   └── pyproject.toml        # name = "agent-cad"
│   │
│   └── slicer/                   # NEW
│       ├── backend/
│       │   └── app/              # 3MF model, plate manager, slicer CLI, printer LAN
│       ├── frontend/             # plate viewer + tabs
│       ├── run.py
│       └── pyproject.toml        # name = "agent-slicer"
│
├── shared/                       # NEW — extracted common code
│   ├── python/                   # agent_core, pywebview shell, events bus, settings infra
│   └── frontend/                 # chat panel, drawing dialog, attachment hooks
│
├── docs/
├── scripts/                      # installers updated to install BOTH apps
└── pyproject.toml                # workspace-level optional; primarily for tooling
```

### Why monorepo, not two repos?

- ~50% of the code is shared (chat panel, drawing dialog, agent runner
  shell, pywebview bridge). Cross-repo extraction means publishing an
  internal package; that's overkill for v0.2.0.
- One git history, one release cycle, one CI workflow that builds both.
- Single installer (`scripts/install.{ps1,sh}`) installs both apps,
  registers both Start Menu entries.
- Splitting later is trivial; merging back is not.

### Why apps/ + shared/, not flat?

The current `backend/app/` becomes `apps/cad/backend/app/`. The
mechanical move is one big rename PR; after that the structure is
boring. Without `apps/`, it's not obvious where slicer code goes
("backend/slicer/?  slicer_backend/?"). With `apps/`, the parallelism
is self-documenting: each app is a peer.

`shared/` is allowed to be initially small — extract things only when
the second app actually needs them. Premature extraction produces
cleanup churn.

## What gets shared

| Component | Today | Split into |
|---|---|---|
| `claude_agent_sdk` plumbing (query loop, message handling) | `backend/app/agent/runner.py` | `shared/python/agent_core/` |
| Tool registration helpers | `backend/app/agent/tools.py` | `shared/python/agent_core/tools.py` (helpers) + per-app toolsets |
| pywebview shell, JS API bridge | `backend/app/main.py` + `api.py` | `shared/python/pywebview_shell/` |
| Events bus | `backend/app/events.py` | `shared/python/events.py` |
| User settings (model, effort, project dir) | `backend/app/settings.py` | `shared/python/settings.py` |
| Chat panel, drawing dialog, attachments, todos | `frontend/src/components/{ChatPanel,DrawingDialog}.tsx`, `frontend/src/lib/chat.ts` | `shared/frontend/chat/` |
| Markdown renderer | `frontend/src/components/Markdown.tsx` | `shared/frontend/` |
| pywebview JS bridge helper | `frontend/src/lib/pywebview.ts` | `shared/frontend/` |
| Permissions UI | `backend/app/permissions.py` + chat permission cards | `shared/python/` + `shared/frontend/chat/` |

## What stays per-app

**Agent CAD only:**
- CADQuery script runner, project/object model, sketches, parameters
- Reference imports (STL/STEP/3MF view-only) — agent uses these as
  measurement sources for new geometry
- Timeline + git-per-turn
- 3D viewer with face/edge/vertex picking
- Sketchfab integration
- Playwright integration (datasheet lookups)

**Agent Slicer only:**
- 3MF parser/writer (lib3mf or trimesh-based)
- Plate manager (in-memory model + 3MF round-trip)
- Plate viewer (multi-plate Three.js scene)
- Slicer CLI invocation (currently just Bambu Studio CLI)
- Printer LAN protocols (currently just Bambu MQTT/FTP)
- Print presets, slice progress, send-to-printer flow
- Live print monitoring (chamber camera, MQTT progress)

The CAD app keeps its current `backend/app/printing/` for *one release
window* so users can still print from CAD while Agent Slicer matures.
Once Agent Slicer ships, the CAD print phase + tools come out.

## CAD → Slicer handoff

Two cases:

### Case A: single object

User clicks **"Slice this"** (replaces the current "Print" entry on the
active object). Agent CAD:

1. Runs the active object through CADQuery to verify it builds.
2. Exports as STL into the project's `exports/<object>-<sha>.stl`.
3. Spawns Agent Slicer with `agent-slicer <stl-path>`.
4. Agent Slicer detects no existing 3MF for this STL, creates a new
   in-memory project rooted at `<project>/slice/<object>-<sha>/`,
   imports the STL onto Plate 1, opens the window.

### Case B: multi-object scene

User has multiple visible objects in the CAD viewer and asks the agent
"slice these all together". Agent CAD:

1. Runs each object, collects STL-like exports into a temp dir.
2. Optionally constructs a starter 3MF (multiple `<object>` entries on
   one plate) so the slicer opens with everything pre-arranged.
3. Spawns the slicer with the 3MF path.

### Wire protocol

Agent Slicer's CLI accepts:

```
agent-slicer [path]                    # open existing 3MF, or import STL/STEP
agent-slicer --new <project_dir>       # fresh empty project
agent-slicer --add <plate> <file>      # append to an already-open instance (IPC TBD)
```

For v0.2.0 we don't need IPC into a running instance — Agent CAD just
spawns a fresh slicer per slice action. If the user already has the
slicer open with an unsaved project, the new spawn opens a second
window. We can add single-instance IPC in v0.3.0 if it's a real pain
point.

## Slicer data model

### 3MF as source of truth

A 3MF (.3mf) is a zip with:

- `[Content_Types].xml`, `_rels/.rels`, `3D/3dmodel.model` — the model
  geometry + initial transforms
- `Metadata/Slic3r_PE_model.config` (PrusaSlicer) /
  `Metadata/model_settings.config` (Bambu Studio) — extension data
  with per-object plate assignment, support flags, etc.

Agent Slicer reads/writes both shapes; we standardize internally on
the Bambu Studio shape because that's the slicer we're driving.

### In-memory model

```python
@dataclass
class SlicerProject:
    path: Path                     # the 3mf on disk
    plates: list[Plate]            # 1..N plates
    active_plate_index: int

@dataclass
class Plate:
    index: int                     # 1-based, matches Bambu UI
    bed: BedSpec                   # size, shape, brand-specific
    models: list[PlacedModel]
    preset: str | None             # slicer preset name
    overrides: list[Override]      # user-set overrides
    last_slice: SliceResult | None

@dataclass
class PlacedModel:
    id: str                        # stable across saves
    name: str                      # display name
    geometry_ref: str              # path inside the 3MF zip
    transform: Mat4                # rotation + translation, no scale
    color: str | None
    settings: ModelSettings        # supports, infill override, etc.
```

Mutations (move, duplicate, orient, change-plate) update the in-memory
model; the 3MF is rewritten on save.

### Plate UI

Tab strip at the top: `[ 1 ] [ 2 ] [ 3 ] [ + ]`. Active plate
highlighted. Right-click on a tab → Rename, Duplicate, Delete.

Main viewport: Three.js scene with the active plate's bed visualized
(printable area as a translucent rectangle, mid-plane grid, origin
gizmo). Models sit on the bed at their stored transforms.

Selection: click model → highlight + show transform handles. Drag to
translate within the bed plane. Right-click for context menu (Rotate,
Center, Move to Plate N, Duplicate, Delete).

Right side: properties panel (selected model's name, position, rotation,
size, slicer overrides). Below it, the chat panel.

## Agent tool surface (slicer)

The agent has full control. Naming convention `mcp__slicer__*`.

**Project / plate ops**
- `list_plates() -> [{index, name, model_count, sliced}]`
- `set_active_plate(index)`
- `add_plate() -> index`
- `remove_plate(index)`
- `rename_plate(index, name)`

**Model ops**
- `list_models(plate?) -> [{id, name, plate, position, rotation, bbox}]`
- `add_model(file_path, plate?)` — import STL/STEP/3MF onto a plate
- `remove_model(model_id)`
- `duplicate_model(model_id, count?)` — places copies on the same plate
- `move_model(model_id, position={x,y,z})`
- `rotate_model(model_id, axis, degrees)`
- `orient_to_face(model_id, face_normal)` — drop onto a chosen face
- `auto_arrange(plate)` — pack models on a plate
- `move_to_plate(model_id, plate_index)`

**Visual evaluation**
- `snapshot(plate?, view?, camera?)` — render the active plate scene
- `scene_snapshot(plates=[...])` — multi-plate composite
- `select(model_id)` / `selection_snapshot()`

**Slicing**
- `set_preset(plate, preset_name)`
- `add_override(plate, key, value, note)`
- `clear_overrides(plate)`
- `slice(plate)` — invokes Bambu Studio CLI, returns `SliceResult`
- `gcode_estimate(plate)` — ask the printer for time/filament estimate

**Printer**
- `list_printers()`
- `send(plate, printer_id)`
- `print_status(printer_id?)`
- `printer_snapshot(printer_id?)`

The CAD agent keeps its CAD tools; CAD loses the print toolset
(`mcp__cad__slice_for_print`, `set_print_preset`, etc.) once the
slicer ships. Until then it stays as a fallback.

## Implementation plan

Each phase is one PR. Each lands behind `release/v0.2.X` — none touch
v0.1.0.

### Phase 0 — design lock-in (THIS doc)
PR title: *Design: split agent-cad into cad + slicer*
Just this `docs/agent-slicer.md`. Reviewer (you) sanity-checks the
architecture and edits anything off-track. No code yet.

### Phase 1 — repo reshape
PR: *Move backend/ frontend/ to apps/cad/*
Pure relocation. No new features. Updates `run.py`, the dev_server,
installers, release workflow paths. CI green = done. ~1 day.

### Phase 2 — extract shared/
PR: *Extract shared chat panel + agent shell*
Move identified shared modules into `shared/`. Update imports in
`apps/cad/`. Still no slicer. Ensures `shared/` interface is exercised
before a second consumer needs it. ~2 days.

### Phase 3 — slicer skeleton
PR: *agent-slicer: skeleton*
A `apps/slicer/` directory that opens a window, mounts the chat panel
from `shared/`, has an empty viewer. Run via `python apps/slicer/run.py`.
No 3MF logic yet. ~1 day.

### Phase 4 — 3MF model + plate manager (no UI)
PR: *agent-slicer: 3mf project model*
Backend can load a 3MF, list plates and models, mutate them, save.
Tests with sample 3MFs from Bambu Studio. ~3 days.

### Phase 5 — plate viewer
PR: *agent-slicer: plate viewer*
Three.js scene with bed + models for the active plate. Tab strip.
Click selection. Read-only at this point. ~2 days.

### Phase 6 — interactivity
PR: *agent-slicer: drag, rotate, move-to-plate*
Translation handles, rotation handles, context menu, keyboard shortcuts,
move-between-plate UI. ~3 days.

### Phase 7 — agent toolset
PR: *agent-slicer: slicer agent tools*
Wire the `mcp__slicer__*` tools into the agent runner. CAD-style
chat-driven flows: "rotate this 90°", "move this to plate 2", etc.
~2 days.

### Phase 8 — slice + send
PR: *agent-slicer: slice and send to printer*
Move `backend/app/printing/{slicers,printers}.py` into
`apps/slicer/backend/app/`. Hook them up to the new agent tools and
the per-plate UI. Live status the same as v0.1.0. ~2 days.

### Phase 9 — handoff
PR: *agent-cad: slice this*
Add the Slice This button to Agent CAD. Spawns Agent Slicer with the
exported STL/3MF. ~1 day.

### Phase 10 — retire CAD print phase
PR: *agent-cad: retire print phase*
Remove `backend/app/printing/state.py`, the print phase prompt block,
print-phase UI panes, related tools. CAD becomes pure modeling.
Bumps to v0.2.0. ~1 day.

**Total: ~18 working days, 10 PRs.** Roughly 4 calendar weeks at a
sustainable pace, faster if focused.

## Open questions

These should be resolved before Phase 4:

1. **3MF library.** lib3mf (official, C++ + Python bindings, ~30 MB
   wheel) vs. parsing the zip ourselves with `xml.etree` + a `trimesh`
   fallback for geometry. lib3mf is correct but heavy; raw parsing is
   ~300 lines but we have to handle Bambu's extension config ourselves.
   Recommend starting with raw parsing — it's a known shape and we
   already depend on `trimesh` for STL/3MF reading.

2. **Plate scene library.** react-three-fiber (already in CAD) vs.
   raw three.js with a thin React shell. r3f is fine but overkill for
   a scene with ~10 meshes; the perf hit doesn't matter at this scale.
   Stick with r3f for code reuse.

3. **Single-instance launching.** Should "Slice this" detect an open
   slicer and add to it, or always spawn fresh? v0.2.0 = always fresh.
   v0.3.0 may add IPC; defer.

4. **Settings sharing.** Both apps want to know the user's chosen
   model and effort. Same `~/.agent-cad/settings.json` but a new
   `~/.agent-cad/slicer.json` for slicer-specific (default printer,
   default preset)? Or split into `cad.json` and `slicer.json`?
   Recommend: keep model/effort in a shared `agent.json`, app-specific
   in `cad.json` / `slicer.json`. Migrate existing settings.json on
   first launch.

5. **Should agent-cad and agent-slicer share one Claude session or
   each get its own?** Each its own — separate processes, separate
   SDK instances. The user "hands off" by clicking Slice This; the
   slicer chat starts fresh with context about what was just imported.

## Risks

- **Reorg PRs are deceptively expensive.** Phase 1 looks trivial but
  changes hundreds of import paths and breaks every IDE bookmark. Plan
  for one full day of fixup beyond the rename itself.
- **3MF write fidelity.** Bambu Studio is finicky about its config
  metadata. A 3MF we write must round-trip through Bambu Studio
  cleanly — test early and often with real Bambu projects, not just
  files we write and re-read.
- **Two installers, two Start Menu entries.** Update the installer
  scripts to handle both. Don't ship one without the other.
- **Existing projects with print state.** Agent CAD projects that have
  `state.json` print state need a migration path (or get to keep using
  the legacy print phase until they re-cycle).
