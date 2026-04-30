"""printer_snapshot.py — capture one frame from a 3D printer's onboard camera.

Looks up the printer config from `~/.agent-cad/settings.json` (by `--printer-id`,
falling back to `default_printer_id`, falling back to the first printer in the
list) and writes a single JPEG snapshot to disk. Prints the output path to stdout
on success.

Currently supports Bambu Labs X1C in LAN / Developer mode. Other vendors will
add a `fetch_snapshot()` method to their `Printer` subclass and inherit the same
CLI shape.

Self-bootstraps into the project venv if invoked with the system Python — same
pattern as `render_snapshot.py` and `mcp_server.py`, so a bare `python
backend/scripts/printer_snapshot.py` works on macOS, Linux, and Windows without
the caller needing to know whether the venv lives at `.venv/bin/python` or
`.venv\\Scripts\\python.exe`.

Examples:
    # Default printer, snapshot to a temp file
    python backend/scripts/printer_snapshot.py

    # A specific printer in settings, explicit output path
    python backend/scripts/printer_snapshot.py --printer-id main --out /tmp/snap.jpg

    # Inline credentials (handy when troubleshooting auth)
    python backend/scripts/printer_snapshot.py --ip 192.168.1.50 --access-code 12345678
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
    """If a project venv exists and we're not already inside it, re-exec
    so imageio-ffmpeg / paho-mqtt / app.* resolve. Mirrors mcp_server.py."""
    venv_subdir = "Scripts" if os.name == "nt" else "bin"
    venv_exe = "python.exe" if os.name == "nt" else "python"
    venv_py = _REPO_ROOT / ".venv" / venv_subdir / venv_exe
    # On macOS / Linux .venv/bin/python is a symlink to the system Python,
    # so resolve()-equality wrongly reports "already in venv". Compare
    # sys.prefix vs sys.base_prefix instead — those diverge only inside
    # a venv, regardless of how the interpreter was invoked.
    already_in_venv = sys.prefix != sys.base_prefix
    if venv_py.exists() and not already_in_venv:
        os.execv(str(venv_py), [str(venv_py), str(_HERE), *sys.argv[1:]])


_reexec_in_venv()
sys.path.insert(0, str(_BACKEND))

from app import settings as app_settings  # noqa: E402
from app.printing.printers import (  # noqa: E402
    BambuLabPrinter,
    BambuPrinterConfig,
)


def _resolve_printer(args: argparse.Namespace) -> BambuLabPrinter:
    if args.ip:
        cfg = BambuPrinterConfig(
            id=args.printer_id or "inline",
            name="(inline)",
            ip=args.ip,
            access_code=args.access_code or "",
        )
        return BambuLabPrinter(cfg)

    s = app_settings.load()
    if not s.printers:
        raise SystemExit(
            "no printers configured in ~/.agent-cad/settings.json. "
            "Add one in Settings, or pass --ip / --access-code."
        )

    target_id = args.printer_id or s.default_printer_id
    if target_id:
        for entry in s.printers:
            if entry.get("id") == target_id:
                return BambuLabPrinter(BambuPrinterConfig(**entry))
        raise SystemExit(
            f"printer id {target_id!r} not found "
            f"(have: {[e.get('id') for e in s.printers]})"
        )

    return BambuLabPrinter(BambuPrinterConfig(**s.printers[0]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Capture a single JPEG frame from a 3D printer's onboard camera. "
            "Currently Bambu X1C / LAN mode only."
        ),
    )
    ap.add_argument(
        "--printer-id",
        help="Printer id from settings.json. Defaults to default_printer_id, "
             "then the first printer in the list.",
    )
    ap.add_argument(
        "--ip",
        help="Inline printer IP. Bypasses settings; pair with --access-code.",
    )
    ap.add_argument(
        "--access-code",
        help="Inline LAN access code (only valid with --ip).",
    )
    ap.add_argument(
        "--out",
        help="Output JPEG path. Defaults to a temp file; the chosen path is "
             "printed to stdout on success.",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Total budget in seconds for the capture (default 15).",
    )
    args = ap.parse_args(argv)

    if args.access_code and not args.ip:
        ap.error("--access-code requires --ip")

    out = (
        Path(args.out)
        if args.out
        else Path(tempfile.gettempdir()) / f"printer-snap-{uuid.uuid4().hex[:8]}.jpg"
    )

    printer = _resolve_printer(args)
    ok, msg = printer.fetch_snapshot(out, timeout=args.timeout)
    if not ok:
        print(msg, file=sys.stderr)
        return 1

    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
