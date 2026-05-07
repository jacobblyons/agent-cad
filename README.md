# Agent CAD

> **⚠️ Experimental — beta software.** v0.1.0 is the first public
> release. APIs, file formats, and behavior may change between
> versions; expect rough edges. File issues at
> [github.com/jacobblyons/agent-cad/issues](https://github.com/jacobblyons/agent-cad/issues).

LLM-driven parametric CAD desktop app. Claude drives CADQuery; you see and
edit the model in a Claude-style chat + 3D viewer UI.

### Agentic coding experience with CAD specific tooling
- agent can download reference models, research dimensions, make plans, and more.
<img width="1248" height="1027" alt="image" src="https://github.com/user-attachments/assets/21002111-1e06-4bb4-9524-ca8882df7a8e" />

<img width="1782" height="1020" alt="image" src="https://github.com/user-attachments/assets/b847cb9f-0f7a-4bd2-a0d4-71b92c4a133b" />


## Install

Releases are published on the
[GitHub Releases page](https://github.com/jacobblyons/agent-cad/releases).
Each release includes:
- `agent-cad-vX.Y.Z.zip` — recommended for Windows
- `agent-cad-vX.Y.Z.tar.gz` — recommended for macOS / Linux
  (preserves executable bits on the install scripts)

After installing, **authenticate to Claude** — pick one:
- `claude login` (Claude Pro / Max subscription), or
- set `ANTHROPIC_API_KEY` in your shell profile / user environment
  (pay-as-you-go API access).

### Windows

1. Download the `.zip`, extract it.
2. Right-click `scripts\install.ps1` → **Run with PowerShell**, or:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
   ```
   The installer offers to install missing prerequisites via `winget`
   (Python 3.12, Node.js LTS if the frontend isn't pre-built, the
   `claude` CLI), then creates a venv, installs Python deps, and adds
   **Agent CAD** shortcuts to the Start Menu and Desktop.
3. Launch from the **Agent CAD** Start Menu shortcut.

Uninstall: `powershell -ExecutionPolicy Bypass -File .\scripts\uninstall.ps1`

### macOS

1. Download the `.tar.gz` and extract it (`tar -xzf agent-cad-vX.Y.Z.tar.gz`).
2. From the extracted folder:
   ```bash
   ./scripts/install.sh
   ```
   The installer uses Homebrew to install missing prerequisites
   (Python 3.12, Node.js LTS if the frontend isn't pre-built, the
   `claude` CLI). It creates a venv, installs Python deps, and marks
   `scripts/Launch-AgentCAD.command` as executable so you can drag it
   to **/Applications** or your Dock.
3. Double-click `scripts/Launch-AgentCAD.command` to start the app.

Uninstall: `./scripts/uninstall.sh`

### Linux

1. Download the `.tar.gz` and extract it.
2. Install Python 3.12 (or 3.11) and Node.js LTS via your package
   manager — the installer doesn't try to do this for you to avoid
   distro-specific surprises:
   ```bash
   # Debian / Ubuntu
   sudo apt install python3.12 python3.12-venv nodejs npm
   # Fedora
   sudo dnf install python3.12 nodejs npm
   # Arch
   sudo pacman -S python nodejs npm
   ```
3. From the extracted folder:
   ```bash
   ./scripts/install.sh
   ```
   This creates a venv, installs Python deps, optionally installs the
   `claude` CLI globally via `npm`, and writes a
   `~/.local/share/applications/agent-cad.desktop` entry so **Agent CAD**
   appears in your application launcher.
4. Launch **Agent CAD** from your application menu.

Uninstall: `./scripts/uninstall.sh`

## Stack
- **Python 3.12** — host process
- **CADQuery** — geometry kernel (OCCT under the hood)
- **Claude Agent SDK** — LLM agent + tool calls (Opus / Sonnet / Haiku)
- **pywebview** — desktop window (uses Edge WebView2 on Windows)
- **React + Vite + TypeScript + Tailwind + shadcn/ui** — UI
- **react-three-fiber** — 3D viewer (face / edge / vertex picking + pinning)
- **VTK** — offscreen snapshot rendering for the agent
- **PyInstaller** — packaging (planned)

## Layout
```
backend/app/        Python — pywebview host, CAD executor, agent runner, MCP tools
frontend/           React app loaded in the webview
docs/               Design notes
run.py              Single-command launcher (dev or prod)
dev_server.py       Vite child-process supervisor (Windows job-bound)
```

## Run from source (developer install)

If you cloned the repo (rather than downloading a release zip) and want
to run from source, one command from the repo root:

```bash
python run.py             # dev:  vite + pywebview window
python run.py --prod      # prod: serves the built bundle (auto-builds if missing)
python run.py --build     # rebuild frontend, then prod
python run.py --kill-port # nuke whatever is on the dev port
```

`run.py` self-bootstraps into `.venv/` if you invoke it with the system
Python, so a fresh shell needs no activation step.

You'll still need Node, the `claude` CLI, and Claude auth as described
in the **Install** section above.

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

**Beta.** v0.1.0 is the first public release — early but functional.
Things will break, names will change, the on-disk project format may
evolve. Use it; don't bet a deadline on it. See
[CHANGELOG.md](CHANGELOG.md) for what shipped. Design notes in
`docs/architecture.md`.
