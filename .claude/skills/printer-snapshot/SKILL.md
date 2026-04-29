---
name: printer-snapshot
description: Capture a single JPEG frame from a 3D printer's onboard chamber camera (currently Bambu X1C / LAN mode only). Use to visually check a running print, verify the build plate is clear before sending a new job, see whether a part has come loose or the nozzle has crashed after an error, or confirm what the printer thinks is loaded in its AMS slot.
---

# printer-snapshot

Pulls one frame from the printer's RTSPS camera stream and writes it as a JPEG. Output path is printed to stdout on success — read it with the `Read` tool to view the image.

Backed by `BambuLabPrinter.fetch_snapshot()`, which shells out to the bundled `imageio-ffmpeg` binary so users don't need a system ffmpeg on PATH.

## How to invoke

Run from the repo root with the project venv:

```bash
.venv/Scripts/python.exe backend/scripts/printer_snapshot.py [options]
```

With no options, uses the default printer from `~/.agent-cad/settings.json`.

## Common patterns

```bash
# Default printer, snapshot to a temp file
.venv/Scripts/python.exe backend/scripts/printer_snapshot.py

# A specific printer in settings, fixed output path
.venv/Scripts/python.exe backend/scripts/printer_snapshot.py \
    --printer-id main --out /tmp/printer-now.jpg

# Inline credentials (handy when troubleshooting auth before saving the printer to settings)
.venv/Scripts/python.exe backend/scripts/printer_snapshot.py \
    --ip 192.168.1.50 --access-code 12345678
```

After the command prints the JPEG path, view the image with `Read <path>`.

## Options

- `--printer-id <id>` — pick a specific printer when more than one is configured. Default = `default_printer_id`, then the first printer in the list.
- `--ip <ip> --access-code <code>` — bypass settings and connect inline.
- `--out <path.jpg>` — explicit output path. Omit to write to a temp file under the system temp dir.
- `--timeout <secs>` — total budget for the capture (default 15). Bump if the printer takes a while to start streaming.

## When to use

- A long print is running and the user wants to peek without standing up.
- Verifying the build plate is clear before sending a new sliced job.
- The printer reported an error (`spaghetti`, `no filament`, …) and you want to see whether the part has come loose or the nozzle has crashed.
- Confirming the AMS slot the printer thinks is loaded is actually loaded with the right colour/material.

## When NOT to use

- For a CAD model render, use `render-snapshot`.
- For continuous monitoring or a live view, the desktop app's PrintPane is the right surface — this skill returns one frame, not a stream.

## Requirements

- Bambu X1C in **Developer Mode / LAN-only mode** (otherwise the RTSPS port 322 is closed).
- Printer reachable on the same LAN as this machine.
- `imageio-ffmpeg` installed (pulled in by `pip install -e .`).

## Failure modes

- *"snapshot timed out after 15s"* — printer is offline, on a different network, the access code is wrong, or developer mode is off.
- *"ffmpeg failed: …"* — the bundled ffmpeg ran but RTSP setup or TLS failed; the trailing line of stderr is included.
- *"imageio-ffmpeg not installed"* — re-run `pip install -e .` in the project venv.
