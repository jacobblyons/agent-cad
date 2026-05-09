#!/usr/bin/env bash
# macOS double-clickable launcher. Finder runs .command files in Terminal.
# Drag this file to /Applications or your Dock to keep it handy after
# `scripts/install.sh` has run once.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    osascript -e 'display dialog "Agent CAD is not installed yet.\n\nRun scripts/install.sh first." buttons {"OK"} with icon caution with title "Agent CAD"' >/dev/null
    exit 1
fi

cd "$ROOT/apps/cad"
exec "$VENV_PY" run.py --prod
