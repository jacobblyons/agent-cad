#!/usr/bin/env python
"""Launcher for the agent-cad stdio MCP server.

This is the entry point .mcp.json points at — it re-execs into the
project's venv if available and runs the standalone server module.
Wraps the same `python -m app.agent.standalone_server` command but
without baking the venv path into the JSON, so the same `.mcp.json`
works for every contributor regardless of where they cloned the repo.

Stdio is sacred here: we cannot print anything to stdout that isn't a
JSON-RPC message — the MCP client (Claude Code) is parsing every line.
Any startup diagnostics go to stderr so they show up in Claude Code's
MCP server log instead of breaking the protocol.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    here = Path(__file__).resolve()
    repo_root = here.parents[2]                     # .../cc-cad
    backend_root = here.parents[1]                   # .../cc-cad/backend
    venv_subdir = "Scripts" if os.name == "nt" else "bin"
    venv_exe = "python.exe" if os.name == "nt" else "python"
    venv_py = repo_root / ".venv" / venv_subdir / venv_exe

    # Re-exec in the venv if we're not already there. We can't compare
    # Path.resolve() of sys.executable vs the venv's python — on macOS /
    # Linux, .venv/bin/python is a symlink to the system Python, so both
    # resolve to the same real path even when we're running outside the
    # venv. Use sys.prefix vs sys.base_prefix instead: those diverge only
    # inside a venv, regardless of how the interpreter was invoked.
    already_in_venv = sys.prefix != sys.base_prefix

    if venv_py.exists() and not already_in_venv:
        sys.stderr.write(f"[agent-cad-mcp] re-exec via venv: {venv_py}\n")
        return subprocess.call(
            [str(venv_py), str(here), *sys.argv[1:]],
            cwd=str(backend_root),
        )

    # Make `app.*` importable regardless of how we were invoked.
    sys.path.insert(0, str(backend_root))
    try:
        from app.agent.standalone_server import main as serve
    except ImportError as e:
        sys.stderr.write(
            f"[agent-cad-mcp] failed to import standalone_server: {e}\n"
            f"[agent-cad-mcp] sys.path={sys.path}\n"
        )
        return 1
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
