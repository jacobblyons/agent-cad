# Agent CAD

LLM-driven parametric CAD desktop app. Claude drives CADQuery; you see and
edit the model in a Claude-style chat + 3D viewer UI.

### Agentic coding experience with CAD specific tooling
- agent can download reference models, research dimensions, make plans, and more.
<img width="1248" height="1027" alt="image" src="https://github.com/user-attachments/assets/21002111-1e06-4bb4-9524-ca8882df7a8e" />



## Stack
- **Python 3.12** — host process
- **CADQuery** — geometry kernel (OCCT under the hood)
- **Claude Agent SDK** — LLM agent + tool calls (Opus / Sonnet / Haiku)
- **pywebview** — desktop window (uses Edge WebView2 on Windows)
- **React + Vite + TypeScript + Tailwind + shadcn/ui** — UI
- **react-three-fiber** — 3D viewer (face / edge / vertex picking + pinning)
- **VTK** — offscreen snapshot rendering for the agent
- **PyInstaller** — packaging

## Layout
```
backend/app/        Python — pywebview host, CAD executor, agent runner, MCP tools
frontend/           React app loaded in the webview
docs/               Design notes
run.py              Single-command launcher (dev or prod)
dev_server.py       Vite child-process supervisor (Windows job-bound)
```

## Run

One command from the repo root:

```bash
python run.py             # dev:  vite + pywebview window
python run.py --prod      # prod: serves the built bundle (auto-builds if missing)
python run.py --build     # rebuild frontend, then prod
python run.py --kill-port # nuke whatever is on the dev port
```

`run.py` self-bootstraps into `.venv/` if you invoke it with the system
Python, so a fresh shell needs no activation step.

## How it works

A **project** is a directory on disk. It holds one or more **objects**,
each a CADQuery script under `objects/<name>.py` plus its own
`<name>.params.json`. Exactly one object is *active* at a time — the
viewer, Tweaks panel, and most of the agent's tools follow the active
object. The whole project is also a git repo: every chat turn that
produces a working model lands as one commit, and the timeline UI lets
you click any commit to checkout, branch, or diff.

The agent's CAD tool surface (in addition to the SDK's Read / Write /
Edit / Glob / Grep) covers:

- **Build / inspect** — `run_model`, `snapshot`, `measure`,
  `mass_properties`, `check_validity`, `query_faces`, `query_edges`,
  `query_vertices`, `eval_expression`
- **Visual evaluation** — `section_snapshot` (cut with an axis-aligned
  plane), `scene_snapshot` (multi-object), `preview_boolean`
  (transient union/intersection/difference of two objects)
- **Numeric evaluation** — `distance_between` with entity refs like
  `main`, `main.face[7]`, `main.edge[3]`, `main.vertex[0]` (works
  cross-object and within a single object)
- **Object management** — `list_objects`, `create_object`,
  `set_active_object`
- **Parameters** — `set_parameter`, `list_parameters`
- **Timeline** — `git_log`, `commit_turn`

The user can click a face / edge / vertex in the viewer to "pin" it; the
pin (entity index, geomType, world coordinates) rides along with the
next chat message so the agent knows exactly what you're pointing at.

## Status
Early scaffold. See `docs/architecture.md` for the design.
