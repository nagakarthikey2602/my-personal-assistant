<#
.SYNOPSIS
    Launch the desktop assistant or pass CLI flags through to it.

.EXAMPLES
    .\scripts\launch.ps1
    .\scripts\launch.ps1 --cli
    .\scripts\launch.ps1 --command "status"
    .\scripts\launch.ps1 --diagnose --json
    .\scripts\launch.ps1 --voice
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "The .venv interpreter was not found. Run scripts\setup.ps1 first."
}

# The package lives under app/ in the source tree. This does not download code
# or contact the network; it only tells the local interpreter where the package is.
$env:PYTHONPATH = (Join-Path $Root "app") + [IO.Path]::PathSeparator + $env:PYTHONPATH

Push-Location $Root
try {
    & $Python -m personal_assistant @AppArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
