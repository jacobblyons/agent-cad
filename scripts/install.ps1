<#
.SYNOPSIS
    Installs Agent CAD on Windows.

.DESCRIPTION
    Idempotent installer. Verifies / installs prerequisites, sets up the
    Python venv, installs Python deps, builds the frontend if a pre-built
    bundle is not already present, and creates Start Menu + Desktop
    shortcuts.

    Prerequisites it can install for you (via winget):
      - Python 3.12
      - Node.js LTS (only required if the frontend bundle is not pre-built)

    Always required from you outside this script:
      - Authenticate to Claude. After install, run `claude login` in a
        new terminal, or set $env:ANTHROPIC_API_KEY in your user
        environment. The Claude Agent SDK uses this to talk to Claude.

.PARAMETER NoShortcut
    Skip Start Menu / Desktop shortcut creation.

.PARAMETER NonInteractive
    Don't prompt — assume "yes" to install missing prerequisites via
    winget. Useful for CI or unattended installs.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [switch] $NoShortcut,
    [switch] $NonInteractive
)

$ErrorActionPreference = 'Stop'

$ROOT     = Split-Path -Parent $PSScriptRoot
$VENV     = Join-Path $ROOT '.venv'
$VENV_PY  = Join-Path $VENV 'Scripts\python.exe'
$FRONTEND = Join-Path $ROOT 'frontend'
$DIST     = Join-Path $FRONTEND 'dist\index.html'

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Ok  ([string]$msg) { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn2([string]$msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Confirm-Install([string]$what) {
    if ($NonInteractive) { return $true }
    $r = Read-Host "    Install $what now? [Y/n]"
    return ($r -eq '' -or $r -match '^[Yy]')
}

function Test-Command([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
    if (-not (Test-Command 'winget')) {
        throw @"
winget is not available, so this script can't auto-install prerequisites.
Either install App Installer from the Microsoft Store, or install these
manually and re-run:
  - Python 3.12 (https://www.python.org/downloads/)
  - Node.js LTS (https://nodejs.org/)
"@
    }
}

function Find-Python312 {
    # Prefer the `py` launcher with -3.12, fall back to plain python.
    if (Test-Command 'py') {
        try {
            $v = & py -3.12 -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v -match '^3\.12') {
                $exe = & py -3.12 -c "import sys;print(sys.executable)"
                return $exe.Trim()
            }
        } catch { }
    }
    if (Test-Command 'python') {
        try {
            $v = & python -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and ($v -match '^3\.12' -or $v -match '^3\.11')) {
                return (& python -c "import sys;print(sys.executable)").Trim()
            }
        } catch { }
    }
    return $null
}

# 1. Python -----------------------------------------------------------------
Write-Step 'Checking for Python 3.11 / 3.12'
$py = Find-Python312
if (-not $py) {
    Write-Warn2 'Python 3.11 or 3.12 not found.'
    Ensure-Winget
    if (-not (Confirm-Install 'Python 3.12 via winget')) {
        throw 'Python 3.12 is required. Aborting.'
    }
    winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
    # winget puts python on PATH but the *current* shell won't pick it up
    # without a refresh. Re-resolve via a fresh `where`.
    $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('PATH', 'User')
    $py = Find-Python312
    if (-not $py) {
        throw 'Python 3.12 was installed but is not on PATH yet. Open a new shell and re-run install.ps1.'
    }
}
Write-Ok "Python: $py"

# 2. Node (only needed if we have to build the frontend) -------------------
$needNode = -not (Test-Path $DIST)
if ($needNode) {
    Write-Step 'Frontend bundle not pre-built — checking for Node.js'
    if (-not (Test-Command 'npm')) {
        Write-Warn2 'Node.js / npm not found.'
        Ensure-Winget
        if (-not (Confirm-Install 'Node.js LTS via winget')) {
            throw 'Node.js is required to build the frontend. Aborting.'
        }
        winget install --id OpenJS.NodeJS.LTS -e --silent --accept-source-agreements --accept-package-agreements
        $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                    [System.Environment]::GetEnvironmentVariable('PATH', 'User')
        if (-not (Test-Command 'npm')) {
            throw 'Node.js was installed but `npm` is not on PATH yet. Open a new shell and re-run install.ps1.'
        }
    }
    Write-Ok ("npm: " + (Get-Command npm).Source)
} else {
    Write-Step 'Frontend bundle already present — skipping Node check'
}

# 3. Claude Code CLI -------------------------------------------------------
Write-Step 'Checking for Claude Code CLI'
if (Test-Command 'claude') {
    Write-Ok ("claude: " + (Get-Command claude).Source)
} else {
    Write-Warn2 'claude CLI not found. The Claude Agent SDK spawns it as a subprocess.'
    if (Test-Command 'npm') {
        if (Confirm-Install '@anthropic-ai/claude-code globally via npm') {
            npm install -g '@anthropic-ai/claude-code'
            if (-not (Test-Command 'claude')) {
                Write-Warn2 'Installed, but `claude` not on PATH yet. Open a new shell after install finishes.'
            }
        } else {
            Write-Warn2 'Skipping. You can install it later with:  npm i -g @anthropic-ai/claude-code'
        }
    } else {
        Write-Warn2 'Install Node.js first, then:  npm i -g @anthropic-ai/claude-code'
    }
}

# 4. Python venv -----------------------------------------------------------
Write-Step 'Setting up Python virtual environment'
if (-not (Test-Path $VENV_PY)) {
    & $py -m venv $VENV
    if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
}
Write-Ok ".venv at $VENV"

Write-Step 'Upgrading pip and installing Agent CAD'
& $VENV_PY -m pip install --upgrade pip wheel
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed' }
& $VENV_PY -m pip install -e $ROOT
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# 5. Build frontend if needed ---------------------------------------------
if ($needNode) {
    Write-Step 'Installing frontend dependencies'
    Push-Location $FRONTEND
    try {
        npm install
        if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
        Write-Step 'Building frontend bundle'
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'npm run build failed' }
    } finally {
        Pop-Location
    }
}
Write-Ok 'Frontend bundle ready.'

# 6. Shortcuts ------------------------------------------------------------
if (-not $NoShortcut) {
    Write-Step 'Creating Start Menu and Desktop shortcuts'
    $launcher = Join-Path $ROOT 'scripts\Launch-AgentCAD.ps1'
    $iconPath = Join-Path $ROOT 'frontend\dist\favicon.ico'
    if (-not (Test-Path $iconPath)) { $iconPath = $null }

    $WS = New-Object -ComObject WScript.Shell

    $startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Agent CAD.lnk'
    $desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Agent CAD.lnk'

    foreach ($lnk in @($startMenu, $desktop)) {
        $s = $WS.CreateShortcut($lnk)
        $s.TargetPath  = (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe')
        $s.Arguments   = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`""
        $s.WorkingDirectory = $ROOT
        $s.Description = 'Agent CAD'
        if ($iconPath) { $s.IconLocation = $iconPath }
        $s.Save()
    }
    Write-Ok "Start Menu: $startMenu"
    Write-Ok "Desktop:    $desktop"
}

Write-Host ""
Write-Host "Agent CAD is installed." -ForegroundColor Green
Write-Host ""
Write-Host "Next:"
if (-not (Test-Command 'claude')) {
    Write-Host "  1. Open a new terminal so the Claude CLI lands on PATH."
    Write-Host "  2. Run: claude login   (or set ANTHROPIC_API_KEY in your user env)"
    Write-Host "  3. Launch from Start Menu or run scripts\Launch-AgentCAD.ps1"
} else {
    Write-Host "  1. If you haven't yet, run: claude login"
    Write-Host "  2. Launch from Start Menu or run scripts\Launch-AgentCAD.ps1"
}
