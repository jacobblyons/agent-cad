#!/usr/bin/env python
"""Agent CAD launcher.

Usage:
    python run.py             # dev mode: vite dev server + pywebview --dev
    python run.py --prod      # prod mode: serves the built bundle (auto-builds if missing)
    python run.py --build     # build the frontend, then run in prod mode
    python run.py --kill-port # nuke whatever is on the dev port and exit

Self-bootstraps into the project venv if invoked with the system Python.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # so `import dev_server` works after re-exec

import dev_server  # noqa: E402

FRONTEND = ROOT / "frontend"
BACKEND = ROOT / "backend"
DIST_INDEX = FRONTEND / "dist" / "index.html"
DEV_PORT = 5273
DEV_URL = f"http://localhost:{DEV_PORT}"

IS_WIN = os.name == "nt"
VENV_PY = ROOT / ".venv" / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def reexec_in_venv() -> None:
    if not VENV_PY.exists():
        return
    # On macOS / Linux, .venv/bin/python is a symlink back to the system
    # Python, so resolve()-equality wrongly reports "already in venv"
    # when we were actually invoked with the system interpreter. Use
    # sys.prefix vs sys.base_prefix — diverges only inside a venv.
    if sys.prefix != sys.base_prefix:
        return
    sys.exit(subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]]))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run", description="Agent CAD launcher")
    p.add_argument("--prod", action="store_true", help="run against the built bundle")
    p.add_argument("--build", action="store_true", help="build the frontend, then run in prod")
    p.add_argument("--debug", action="store_true", help="open webview devtools in prod mode")
    p.add_argument("--kill-port", action="store_true",
                   help=f"kill whatever is holding port {DEV_PORT} and exit")
    return p.parse_args()


def wait_for_url(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.2)
    raise TimeoutError(f"timed out waiting for {url}")


def build_frontend() -> None:
    print("[run] building frontend…")
    code = subprocess.call(
        [dev_server._npm(), "run", "build"], cwd=str(FRONTEND),
    )
    if code != 0:
        sys.exit(f"frontend build failed (exit {code})")


def kill_port_and_exit() -> int:
    holder = dev_server.who_holds(DEV_PORT)
    if not holder:
        print(f"[run] nothing listening on :{DEV_PORT}")
        return 0
    print(f"[run] killing PID {holder.pid} ({holder.name}) on :{DEV_PORT}")
    dev_server.kill_pid(holder.pid)
    # Wait for it to actually let go.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dev_server.port_open(DEV_PORT):
        time.sleep(0.1)
    if dev_server.port_open(DEV_PORT):
        print(f"[run] warning: :{DEV_PORT} still in use")
        return 1
    print(f"[run] :{DEV_PORT} freed")
    return 0


def explain_busy_port() -> None:
    holder = dev_server.who_holds(DEV_PORT)
    msg = [f"port {DEV_PORT} is already in use"]
    if holder:
        msg.append(f"  held by PID {holder.pid}{' (' + holder.name + ')' if holder.name else ''}")
        msg.append(f"  free it with: python run.py --kill-port")
    sys.exit("\n".join(msg))


def run_dev() -> int:
    if dev_server.port_open(DEV_PORT):
        explain_busy_port()
    print(f"[run] starting vite on :{DEV_PORT} (job-bound, will die with us)")
    proc, job = dev_server.start(FRONTEND, port=DEV_PORT)
    try:
        wait_for_url(DEV_URL, timeout_s=30)
        print(f"[run] vite ready, opening window")
        rc = subprocess.call([sys.executable, "-m", "app.main", "--dev"], cwd=str(BACKEND))
        return rc
    finally:
        dev_server.stop(proc, job)


def run_prod(debug: bool) -> int:
    if not DIST_INDEX.exists():
        build_frontend()
    py_args = [sys.executable, "-m", "app.main"] + (["--debug"] if debug else [])
    return subprocess.call(py_args, cwd=str(BACKEND))


def main() -> int:
    reexec_in_venv()
    args = parse_args()
    if args.kill_port:
        return kill_port_and_exit()
    if args.build:
        build_frontend()
        return run_prod(args.debug)
    if args.prod:
        return run_prod(args.debug)
    return run_dev()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
