<#
.SYNOPSIS
    Removes Agent CAD shortcuts and the project venv.

.DESCRIPTION
    Conservative uninstaller. Does NOT touch:
      - Your projects in %USERPROFILE%\.agent-cad\projects
      - Your settings in %USERPROFILE%\.agent-cad\settings.json
      - Globally installed prerequisites (Python, Node, Claude CLI)

    To fully remove user data, also delete %USERPROFILE%\.agent-cad.
#>
[CmdletBinding()]
param(
    [switch] $KeepVenv
)

$ErrorActionPreference = 'Continue'

$ROOT = Split-Path -Parent $PSScriptRoot
$VENV = Join-Path $ROOT '.venv'

$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Agent CAD.lnk'
$desktop   = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Agent CAD.lnk'

foreach ($lnk in @($startMenu, $desktop)) {
    if (Test-Path $lnk) {
        Remove-Item $lnk -Force
        Write-Host "removed $lnk"
    }
}

if (-not $KeepVenv -and (Test-Path $VENV)) {
    Write-Host "removing $VENV ..."
    Remove-Item $VENV -Recurse -Force
}

Write-Host "Done. Your projects in ~\.agent-cad\projects are untouched."
