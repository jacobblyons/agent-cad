---
name: handoff-to-cad-ui
description: Hand off the current Agent CAD project to the desktop GUI so the user can keep working visually — orbits the model, clicks on faces, edits parameters in the Tweaks panel, etc. Use when the user has been working with the cad-engineer subagent (or directly via mcp__agent-cad__* tools) and asks to "open this in the UI", "switch to the desktop app", "let me see it", "hand this off", or any equivalent. Also use whenever the work has reached a state where visual inspection / interactive tweaking would help.
---

# handoff-to-cad-ui

Launches the Agent CAD desktop app pointing at the project the user
has been working on. The harness is the same one used by the
cad-engineer subagent — just with a chat panel, 3D viewer, sketch
overlays, parameter sliders, timeline, and the print-phase UI on top.
Once the app's running the user can talk to its in-app agent (which
sees the same project files), so context carries forward.

## When to invoke

- User says something like:
  - "let me see it"
  - "open in the UI"
  - "switch to the GUI / desktop"
  - "hand off"
  - "let me take it from here"
- The current state is worth visualising and the user hasn't been
  in the GUI yet.
- They want to interactively tweak parameters or annotate by
  clicking faces — neither is possible from the CLI.

## How to invoke

Run the launcher from the repo root. It self-bootstraps into the
project venv (`.venv/bin/python` on macOS / Linux,
`.venv\Scripts\python.exe` on Windows), opens the desktop window, and
waits — the command keeps running for as long as the app is open, so
launch it in the background.

```bash
python run.py
```

Optional: pass `--prod` to run the built bundle (faster startup, no
Vite dev server). Without `--prod` it spins up the Vite dev server on
:5273 and opens the window against that — useful when the frontend
might have unsaved changes.

```bash
python run.py --prod
```

## Pre-launch checklist

Before launching:

1. **Commit any uncommitted work.** Use the
   `mcp__agent-cad__commit_turn` tool with a short imperative subject
   if there are uncommitted changes. The desktop app shows an
   "uncommitted" dot on the tab and the user usually wants a clean
   handoff. (If the user explicitly said to leave it dirty, skip
   this — they may want to inspect the WIP visually before
   committing.)
2. **Drop the print phase if you're in it.** Call
   `mcp__agent-cad__leave_print_phase`. The desktop app reads phase
   state from a separate channel, so leftover server-side phase
   state from this session won't affect the GUI — but cleaning up
   keeps logs tidy.
3. **Note the project path.** The desktop app remembers recently
   opened projects under `~/.agent-cad/projects/` (and whatever
   `default_project_dir` is set to in settings) — anything you
   opened or created via `mcp__agent-cad__open_project` /
   `create_project` will show up in its "Recent Projects" list. If
   the project lives outside the default dir, tell the user to use
   "Open project (Ctrl+O)" and point at the path.

## Telling the user

After launching, tell the user:

- That the desktop window should be opening (give it a few seconds —
  Vite dev mode takes longer than `--prod`).
- The path of the project they should open / pick from recents.
- A one-liner about what state they'll find (e.g. "uncommitted
  changes are still in the working tree"; "the active object is
  `main`, sketches are visible"; etc.).
- That they can talk to the in-app agent via the chat panel — it has
  the same toolset you've been using here.

## Background launch

Use Bash with `run_in_background: true` so the launch doesn't block
your turn. The app's stdout/stderr stream to the bash output buffer;
you can read it later if something goes wrong.

```
Bash {
  command: "python run.py",
  description: "Launch Agent CAD desktop app",
  run_in_background: true
}
```

## What NOT to do

- Don't try to forward your active project directly — the desktop
  app has its own state-management. Just give the user the project
  path and let them open it.
- Don't kill the running cad-engineer / standalone MCP server. The
  user may want to talk to both (rare, but possible — the in-app
  agent and your stdio session are independent and won't fight over
  files thanks to git's locking on commits).
- Don't `--prod` unless the user asks for it OR `frontend/dist/`
  obviously exists and is recent. Vite dev mode picks up frontend
  edits live, which is usually what the user wants.
