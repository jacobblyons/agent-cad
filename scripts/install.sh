#!/usr/bin/env bash
# Agent CAD installer for macOS and Linux.
#
# Usage:
#   bash scripts/install.sh
#
# Idempotent — safe to re-run to upgrade an existing install. Verifies
# prerequisites, sets up the Python venv, installs Python deps, builds
# the frontend if a pre-built bundle is not already present, and creates
# OS-appropriate launcher integration.
#
# Always required from you (outside this script):
#   - Authenticate to Claude. After install, run `claude login` in a
#     new terminal, or set ANTHROPIC_API_KEY in your shell profile.
#
# Env knobs:
#   NONINTERACTIVE=1   assume "yes" for any install prompts
#   KEEP_VENV=1        skip venv recreation if it exists (default behavior)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT/.venv"
VENV_PY="$VENV/bin/python"
CAD_APP="$ROOT/apps/cad"
FRONTEND="$CAD_APP/frontend"
DIST="$FRONTEND/dist/index.html"

OS="$(uname -s)"   # Darwin / Linux

# --- output helpers -------------------------------------------------------

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m%s\033[0m\n' "$*"; }
warn() { printf '    \033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

confirm() {
    if [ "${NONINTERACTIVE:-0}" = "1" ]; then return 0; fi
    local prompt="$1" reply
    read -r -p "    $prompt [Y/n] " reply
    [ -z "$reply" ] || [[ "$reply" =~ ^[Yy] ]]
}

# Locate a usable Python (3.11 or 3.12). Prints the absolute path on success.
find_python() {
    local cmd v
    for cmd in python3.12 python3.11 python3 python; do
        if command -v "$cmd" >/dev/null 2>&1; then
            v="$("$cmd" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
            if [ "$v" = "3.11" ] || [ "$v" = "3.12" ]; then
                command -v "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# --- 1. Python ------------------------------------------------------------

step "Checking for Python 3.11 / 3.12"
if PY="$(find_python)"; then
    ok "Python: $PY"
else
    warn "Python 3.11 or 3.12 not found."
    if [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            if confirm "Install Python 3.12 via Homebrew?"; then
                brew install python@3.12
                PY="$(find_python)" || die "Python 3.12 install didn't land on PATH. Open a fresh shell and rerun."
                ok "Python: $PY"
            else
                die "Python 3.12 is required. Aborting."
            fi
        else
            die "Install Homebrew (https://brew.sh) or Python 3.12 manually, then rerun."
        fi
    else
        # Linux — distro fragmentation makes auto-install risky. Help, don't act.
        die "Install Python 3.12 (or 3.11) via your package manager, then rerun:
  Debian/Ubuntu : sudo apt install python3.12 python3.12-venv
  Fedora/RHEL   : sudo dnf install python3.12
  Arch          : sudo pacman -S python
  openSUSE      : sudo zypper install python312
"
    fi
fi

# --- 2. Node (only needed if frontend not pre-built) ---------------------

need_node=0
[ -f "$DIST" ] || need_node=1

if [ "$need_node" = 1 ]; then
    step "Frontend bundle not pre-built — checking for Node.js"
    if command -v npm >/dev/null 2>&1; then
        ok "npm: $(command -v npm)"
    else
        warn "npm not found."
        if [ "$OS" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
            if confirm "Install Node.js LTS via Homebrew?"; then
                brew install node
                command -v npm >/dev/null 2>&1 || die "Node install didn't land on PATH. Open a fresh shell and rerun."
            else
                die "Node.js is required to build the frontend. Aborting."
            fi
        else
            die "Install Node.js LTS (https://nodejs.org/), then rerun."
        fi
    fi
else
    step "Frontend bundle already present — skipping Node check"
fi

# --- 3. Claude CLI -------------------------------------------------------

step "Checking for Claude CLI"
if command -v claude >/dev/null 2>&1; then
    ok "claude: $(command -v claude)"
elif command -v npm >/dev/null 2>&1; then
    if confirm "Install @anthropic-ai/claude-code globally via npm?"; then
        npm install -g @anthropic-ai/claude-code
        if ! command -v claude >/dev/null 2>&1; then
            warn "Installed, but 'claude' is not on PATH yet. Open a fresh shell after this finishes."
        fi
    else
        warn "Skipping. You can install it later with:  npm i -g @anthropic-ai/claude-code"
    fi
else
    warn "Install Node.js first, then:  npm i -g @anthropic-ai/claude-code"
fi

# --- 4. Python venv + install -------------------------------------------

step "Setting up Python virtual environment"
if [ ! -x "$VENV_PY" ]; then
    "$PY" -m venv "$VENV"
fi
ok ".venv at $VENV"

step "Upgrading pip and installing Agent CAD"
"$VENV_PY" -m pip install --upgrade pip wheel
"$VENV_PY" -m pip install -e "$CAD_APP"

# --- 5. Build frontend if needed ----------------------------------------

if [ "$need_node" = 1 ]; then
    step "Installing frontend dependencies"
    (cd "$FRONTEND" && npm install)
    step "Building frontend bundle"
    (cd "$FRONTEND" && npm run build)
fi
ok "Frontend bundle ready."

# --- 6. OS-specific launcher integration --------------------------------

if [ "$OS" = "Darwin" ]; then
    chmod +x "$ROOT/scripts/Launch-AgentCAD.command"
    ok "macOS launcher: $ROOT/scripts/Launch-AgentCAD.command"
    ok "Drag it to /Applications or your Dock to keep it handy."
else
    chmod +x "$ROOT/scripts/launch-agent-cad.sh"
    apps_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
    mkdir -p "$apps_dir"
    desktop_file="$apps_dir/agent-cad.desktop"
    cat > "$desktop_file" <<EOF
[Desktop Entry]
Type=Application
Name=Agent CAD
Comment=LLM-driven parametric CAD
Exec=$ROOT/scripts/launch-agent-cad.sh
Path=$ROOT
Terminal=false
Categories=Development;Graphics;
EOF
    ok "Linux app entry: $desktop_file"
fi

# --- 7. Done ------------------------------------------------------------

cat <<EOF

Agent CAD is installed.

Next:
  1. If you haven't yet, run: claude login   (or set ANTHROPIC_API_KEY)
EOF
if [ "$OS" = "Darwin" ]; then
    echo "  2. Launch by double-clicking scripts/Launch-AgentCAD.command,"
    echo "     or run: $VENV_PY $CAD_APP/run.py --prod"
else
    echo "  2. Launch from your application menu (Agent CAD),"
    echo "     or run: $VENV_PY $CAD_APP/run.py --prod"
fi
