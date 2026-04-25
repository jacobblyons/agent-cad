"""Application entrypoint.

Modes:
  --dev   Load the Vite dev server at http://localhost:5173.
  (none)  Load the built frontend from frontend/dist/index.html.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import webview

from .api import JsApi
from .events import bus

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = REPO_ROOT / "frontend" / "dist" / "index.html"
DEV_URL = "http://localhost:5273"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="agent-cad")
    p.add_argument("--dev", action="store_true", help="load Vite dev server")
    p.add_argument("--debug", action="store_true", help="open webview devtools")
    return p.parse_args(argv)


def resolve_url(dev: bool) -> str:
    if dev:
        return DEV_URL
    if not FRONTEND_DIST.exists():
        sys.stderr.write(
            f"frontend bundle not found at {FRONTEND_DIST}\n"
            "run `cd frontend && npm run build` first, or pass --dev.\n"
        )
        sys.exit(2)
    return FRONTEND_DIST.as_uri()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    api = JsApi()
    window = webview.create_window(
        title="Agent CAD",
        url=resolve_url(args.dev),
        js_api=api,
        width=1400,
        height=900,
        min_size=(900, 600),
    )

    def on_started() -> None:
        bus.attach(window)

    webview.start(on_started, debug=args.debug or args.dev)
    bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
