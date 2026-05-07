#!/usr/bin/env bash
# Removes Agent CAD launcher integration and the project venv.
#
# Conservative — does NOT touch:
#   - Your projects in ~/.agent-cad/projects
#   - Your settings in ~/.agent-cad/settings.json
#   - Globally installed prerequisites (Python, Node, Claude CLI)
#
# Env knobs:
#   KEEP_VENV=1   leave .venv in place (only remove launcher integration)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OS="$(uname -s)"

if [ "$OS" = "Linux" ]; then
    desktop="${XDG_DATA_HOME:-$HOME/.local/share}/applications/agent-cad.desktop"
    if [ -f "$desktop" ]; then
        rm "$desktop"
        echo "removed $desktop"
    fi
fi
# Nothing to remove on macOS — the .command file lives inside scripts/
# and goes away when the user deletes the project folder.

if [ "${KEEP_VENV:-0}" != "1" ] && [ -d "$ROOT/.venv" ]; then
    echo "removing $ROOT/.venv ..."
    rm -rf "$ROOT/.venv"
fi

echo "Done. Your projects in ~/.agent-cad/projects are untouched."
