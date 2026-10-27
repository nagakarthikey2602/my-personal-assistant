<#
.SYNOPSIS
    Run installation diagnostics, the offline guard check, or the built-in
    functional self-test.

.DESCRIPTION
    These checks are local and read-only except for a temporary write probe and
    a loopback socket used by the diagnostic itself.

.PARAMETER Json
    Emit JSON for diagnostics or the offline check.

.PARAMETER Offline
    Check that the Python socket guard blocks external traffic and permits loopback.

.PARAMETER SelfTest
    Run the built-in end-to-end self-test in a temporary sandbox.
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [switch]$Offline,
    [switch]$SelfTest
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "The .venv interpreter was not found. Run scripts\setup.ps1 first."
}

$env:PYTHONPATH = (Join-Path $Root "app") + [IO.Path]::PathSeparator + $env:PYTHONPATH
$ModeArgs = @()
if ($Offline) {
    $ModeArgs += "--offline-check"
} elseif ($SelfTest) {
    $ModeArgs += "--self-test"
} else {
    $ModeArgs += "--diagnose"
}
if ($Json -and -not $SelfTest) {
    $ModeArgs += "--json"
}

Push-Location $Root
try {
    & $Python -m personal_assistant @ModeArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
