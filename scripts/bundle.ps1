<#
.SYNOPSIS
    Build a portable source bundle without personal runtime data.

.DESCRIPTION
    The bundle contains the application, tests, documentation, scripts, the
    locally trained intent model, and the synthetic training/evaluation data.
    It deliberately excludes .venv, memory databases, logs, sandbox files,
    user-recorded voice templates, the optional Vosk and Whisper speech models,
    and configuration overrides.

.PARAMETER Version
    Optional version override. Defaults to __version__ in the package.

.PARAMETER KeepStage
    Keep the expanded staging directory after creating the zip.
#>
[CmdletBinding()]
param(
    [string]$Version = "",
    [switch]$KeepStage
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if (-not $Version) {
    $Init = Get-Content -LiteralPath (Join-Path $Root "app\personal_assistant\__init__.py") -Raw
    if ($Init -match '__version__\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        throw "Could not determine the package version."
    }
}

$StageName = "LocalPersonalAssistant-$Version-portable"
$Stage = Join-Path $Root "dist\$StageName"
$Zip = Join-Path $Root "dist\$StageName.zip"

function Copy-FilteredTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (-not (Test-Path -LiteralPath $Source)) {
        Write-Warning "Skipping missing path: $Source"
        return
    }
    Get-ChildItem -LiteralPath $Source -Recurse -File | ForEach-Object {
        $Relative = $_.FullName.Substring($Source.Length).TrimStart('\')
        if ($Relative -match '(^|\\)__pycache__(\\)' -or $_.Extension -in @(".pyc", ".pyo")) {
            return
        }
        $Target = Join-Path $Destination $Relative
        $Parent = Split-Path -Parent $Target
        New-Item -ItemType Directory -Force -Path $Parent | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $Target -Force
    }
}

if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

foreach ($Directory in @("app", "docs", "scripts", "tests", "packaging")) {
    Copy-FilteredTree -Source (Join-Path $Root $Directory) -Destination (Join-Path $Stage $Directory)
}
Copy-FilteredTree -Source (Join-Path $Root "training_data\generated") -Destination (Join-Path $Stage "training_data\generated")

foreach ($File in @(
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.lock.txt",
    ".gitignore"
)) {
    $Source = Join-Path $Root $File
    if (Test-Path -LiteralPath $Source) {
        Copy-Item -LiteralPath $Source -Destination (Join-Path $Stage $File) -Force
    }
}

$ConfigDir = Join-Path $Stage "config"
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
Copy-Item -LiteralPath (Join-Path $Root "config\config.example.yaml") -Destination (Join-Path $ConfigDir "config.example.yaml") -Force

$ModelDir = Join-Path $Stage "models"
New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
Get-ChildItem -LiteralPath (Join-Path $Root "models") -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "intent_*" } |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ModelDir $_.Name) -Force }

$StartHere = @"
Local Personal Assistant $Version — portable source bundle
Generated: $(Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")

This bundle contains no personal memory, logs, voice recordings, or configuration
overrides. The intent model and synthetic training data are included for
reproducibility. User-recorded speech templates are intentionally excluded.

The optional Vosk (about 40 MB) and Whisper (about 75 MB) speech models are
also excluded. To use local general speech recognition or the 'hearty' wake
word, install them once after setup:
  .venv\Scripts\python.exe .\scripts\install_voice_model.py
  .venv\Scripts\python.exe .\scripts\install_whisper_model.py
Then choose speech.asr_engine: vosk or whisper in config\config.yaml. Wake
detection always uses Vosk; wake_command_engine: whisper gives more accurate
hands-free commands. The running assistant never downloads a model and never
sends audio off this PC.

Quick start (PowerShell):
  1. Install CPython 3.11-3.14 (this build was exercised on 3.14.6).
  2. Run: powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
     Setup is the only step that may need pip network access. Use -Offline and
     a local wheelhouse if the machine has no network.
  3. Run: powershell -ExecutionPolicy Bypass -File .\scripts\launch.ps1
  4. Run diagnostics: .\scripts\diagnose.ps1
  5. Run tests: .\scripts\test.ps1

Read docs\SECURITY.md and docs\CAPABILITIES.md before granting broader file
access or enabling network browser navigation. The application never silently
falls back to cloud services or downloads models.
"@
Set-Content -LiteralPath (Join-Path $Stage "START_HERE.txt") -Value $StartHere -Encoding utf8

$HashFile = Join-Path $Stage "checksums.sha256"
$HashLines = Get-ChildItem -LiteralPath $Stage -Recurse -File |
    Where-Object { $_.Name -ne "checksums.sha256" } |
    Sort-Object FullName |
    ForEach-Object {
        $Relative = $_.FullName.Substring($Stage.Length).TrimStart('\').Replace('\', '/')
        $Digest = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$Digest  $Relative"
    }
Set-Content -LiteralPath $HashFile -Value $HashLines -Encoding ascii

if (Test-Path -LiteralPath $Zip) {
    Remove-Item -LiteralPath $Zip -Force
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "dist") | Out-Null
Compress-Archive -LiteralPath $Stage -DestinationPath $Zip -CompressionLevel Optimal
if (-not $KeepStage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}

Write-Host "Bundle created:"
Write-Host "  $Zip"
Write-Host "  Size: $([math]::Round((Get-Item -LiteralPath $Zip).Length / 1MB, 2)) MB"
Write-Host "The bundle excludes personal data, user-recorded voice templates, and the optional Vosk model."
