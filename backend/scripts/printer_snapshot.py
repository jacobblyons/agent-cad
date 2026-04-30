"""Grab a single PNG frame from a configured Bambu printer's camera.

Thin CLI around `app.printing.camera.grab_frame`. The X1C exposes its
chamber cam as a self-signed RTSPS stream on port 322; that module
handles the FFmpeg flags + first-keyframe dance.

Self-bootstraps into the project venv if invoked with the system
Python — same pattern as `render_snapshot.py` and `mcp_server.py`, so
a bare `python backend/scripts/printer_snapshot.py` works on macOS,
Linux, and Windows without the caller needing to know the venv path.

Usage:
    python backend/scripts/printer_snapshot.py [--printer <id>] [--out <path>]
                                               [--frames N] [--timeout SECS]

On success, writes the PNG and prints its absolute path on stdout.
On failure, prints a one-line reason on stderr and exits non-zero.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_BACKEND = _HERE.parents[1]
_REPO_ROOT = _HERE.parents[2]


def _reexec_in_venv() -> None:
    venv_subdir = "Scripts" if os.name == "nt" else "bin"
    venv_exe = "python.exe" if os.name == "nt" else "python"
    venv_py = _REPO_ROOT / ".venv" / venv_subdir / venv_exe
    already_in_venv = sys.prefix != sys.base_prefix
    if venv_py.exists() and not already_in_venv:
        os.execv(str(venv_py), [str(venv_py), str(_HERE), *sys.argv[1:]])


_reexec_in_venv()
sys.path.insert(0, str(_BACKEND))

from app.printing.camera import grab_frame  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Grab a single PNG frame from the configured Bambu printer's camera.",
    )
    ap.add_argument("--printer", help="Printer id from settings.json. Defaults to default_printer_id.")
    ap.add_argument("--out", help="Output PNG path. Omit to write to a temp file; the path is printed on stdout.")
    ap.add_argument("--frames", type=int, default=15,
                    help="How many frames to read before keeping the last one. Default: 15.")
    ap.add_argument("--timeout", type=float, default=10.0,
                    help="Hard cap on time spent reading frames (seconds). Default: 10.")
    args = ap.parse_args(argv)

    res = grab_frame(printer_id=args.printer, frames=args.frames, timeout=args.timeout)
    if not res.ok:
        print(res.error, file=sys.stderr)
        return 2

    out_path = (Path(args.out).resolve() if args.out
                else Path(tempfile.gettempdir()) / f"x1c-snap-{uuid.uuid4().hex[:8]}.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(res.png_bytes or b"")

    print(str(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
