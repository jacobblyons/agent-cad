<#
.SYNOPSIS
    Launches Agent CAD in production mode using the project venv.

.DESCRIPTION
    The Start Menu / Desktop shortcuts created by install.ps1 point at
    this script. It re-execs run.py with the venv Python; run.py
    handles the rest (auto-builds the frontend if missing, opens the
    pywebview window).
#>
$ErrorActionPreference = 'Stop'

$ROOT    = Split-Path -Parent $PSScriptRoot
$VENV_PY = Join-Path $ROOT '.venv\Scripts\python.exe'
$RUN_PY  = Join-Path $ROOT 'apps\cad\run.py'

if (-not (Test-Path $VENV_PY)) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show(
        "Agent CAD isn't installed yet.`r`n`r`nRun scripts\install.ps1 first.",
        'Agent CAD',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning
    ) | Out-Null
    exit 1
}

Set-Location $ROOT
& $VENV_PY $RUN_PY --prod
exit $LASTEXITCODE
