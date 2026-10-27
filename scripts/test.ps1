<#
.SYNOPSIS
    Run the automated test suite in the project virtual environment.
#>
[CmdletBinding()]
param(
    [string]$Pattern = "tests",
    [switch]$VerboseOutput
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "The .venv interpreter was not found. Run scripts\setup.ps1 -Dev first."
}

$TestArgs = @("-m", "pytest", $Pattern, "-q")
if ($VerboseOutput) {
    $TestArgs += "-vv"
}
Push-Location $Root
try {
    & $Python @TestArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
