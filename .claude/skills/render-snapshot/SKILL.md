---
name: render-snapshot
description: Render a PNG screenshot of an agent-cad model from a chosen camera angle, using the same VTK pipeline as the agent's snapshot tool and the desktop viewer. Use when you need to visually inspect a model — verifying geometry, comparing before/after a change, checking a feature from a specific angle, or sanity-checking a script you just wrote/edited.
---

# render-snapshot

Render a CADQuery model to a PNG via the same backend code path the agent's snapshot tool uses (`app.cad.script_runner.snapshot` → `_snapshot_worker` → `app.cad.snapshot.render_png`, VTK offscreen). Output is a real shaded render — useful when the live three.js viewer's lighting can't show what you need to see.

## How to invoke

Run the CLI with the repo's venv. The output PNG path is printed to stdout on success; read it with the `Read` tool to view the image.

```bash
.venv/Scripts/python.exe backend/scripts/render_snapshot.py <input> [options]
```

`<input>` is either:
- a `.py` script that defines a top-level `model = ...` (CADQuery `Workplane` or `Shape`), **or**
- an agent-cad project directory (e.g. `~/.agent-cad/projects/<name>`) — combine with `--object <name>` to pick which object script under `objects/` to render.

The CLI auto-loads sibling sketches/imports the same way the agent does, so a script that reads `sketches["foo"]` or `imports["bar"]` renders correctly.

## Camera

Two ways to point the camera. CADQuery convention applies: **+Z is up, units are mm.**

**Preset views** (`--view`, default `iso`):
- `iso` — isometric, looking from +X / -Y / slightly above
- `front` / `back` — along ±Y
- `left` / `right` — along ∓X
- `top` / `bottom` — along ∓Z

**Custom camera** (overrides `--view`):
- `--position X Y Z` — eye position in world coords (mm)
- `--target X Y Z` — point the camera looks at (default `0 0 0`)
- `--up X Y Z` — up vector (default `0 0 1`)

The viewer's `Snapshot` button copies the live camera as `position=[…], target=[…], up=[…]` in CADQuery coords — paste those numbers straight into `--position` / `--target` / `--up` to render from the same angle.

## Common patterns

```bash
# Quick iso check of a single script
.venv/Scripts/python.exe backend/scripts/render_snapshot.py path/to/widget.py

# Look at one object inside a project, from above
.venv/Scripts/python.exe backend/scripts/render_snapshot.py \
    ~/.agent-cad/projects/my-proj --object base_plate --view top

# Re-render from the same angle the user has on screen
.venv/Scripts/python.exe backend/scripts/render_snapshot.py path/to/widget.py \
    --position 200 -180 140 --target 0 0 30 --up 0 0 1

# Higher-resolution render to a specific path
.venv/Scripts/python.exe backend/scripts/render_snapshot.py widget.py \
    --view front --width 1600 --height 1200 --out /tmp/widget-front.png
```

After the command prints the PNG path, view the image with `Read <path>`.

## Other flags

- `--width N --height N` — output size (default 900×700)
- `--params <path.json>` — params injected as the `params` global. Defaults to `<script>.params.json` next to the script (single-script mode) or `objects/<name>.params.json` (project mode); missing file is treated as empty params.
- `--out <path.png>` — explicit output path. Omit to write to a temp file.
- `--timeout <secs>` — subprocess hard limit (default 30s); bump for very heavy models.

## When NOT to use

- For a quick visual of what's currently on the user's screen, prefer asking the user to use the in-app `Snapshot` button — it captures the live three.js view including their orbit angle.
- For exporting to STL / STEP, this is the wrong tool — that flow is `app.cad.script_runner.export_models`, not snapshot.
