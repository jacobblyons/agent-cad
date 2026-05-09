#!/usr/bin/env bash
# Linux launcher invoked from the .desktop entry written by install.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    msg="Agent CAD is not installed yet.\n\nRun scripts/install.sh first."
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --text="$msg" --title="Agent CAD" 2>/dev/null || true
    elif command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$msg" --title "Agent CAD" 2>/dev/null || true
    else
        printf '%b\n' "$msg" >&2
    fi
    exit 1
fi

cd "$ROOT/apps/cad"
exec "$VENV_PY" run.py --prod
