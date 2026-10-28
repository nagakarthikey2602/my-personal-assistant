<#
.SYNOPSIS
    Build the optional PyInstaller Windows executables.

.DESCRIPTION
    This build is optional and requires the PyInstaller package in the project
    virtual environment. If PyInstaller is not installed, the script reports
    that fact and exits with code 2; it does not fabricate a build.

    The packaged folder contains a desktop executable and a console executable.
    No personal runtime data or user-recorded voice templates are included.
    The intent model is expected in the per-user data directory; see README.md.

.PARAMETER SkipTests
    Do not run the automated suite before building.

.PARAMETER Zip
    Also create a zip of the built folder.
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "The .venv interpreter was not found. Run scripts\setup.ps1 -Dev first."
}

Push-Location $Root
try {
    # A missing optional package must produce a clear status, not a terminating
    # PowerShell NativeCommandError. Capture stderr without treating it as fatal.
    $PreviousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $PyInstallerVersion = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
    $PyInstallerExit = $LASTEXITCODE
    $ErrorActionPreference = $PreviousErrorAction
    if ($PyInstallerExit -ne 0) {
        Write-Host "PyInstaller is not installed in this environment." -ForegroundColor Yellow
        Write-Host "Install it explicitly with:"
        Write-Host "  .venv\Scripts\python.exe -m pip install `"pyinstaller>=6.15,<7`""
        Write-Host "PyInstaller support for CPython 3.14 is not assumed; verify before relying on this build."
        exit 2
    }
    Write-Host "PyInstaller version: $PyInstallerVersion"

    if (-not $SkipTests) {
        Write-Host "Running tests before packaging."
        & $Python -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "Tests failed; the package was not built."
        }
    }

    foreach ($Path in @("build", "dist\LocalPersonalAssistant")) {
        $Full = Join-Path $Root $Path
        if (Test-Path -LiteralPath $Full) {
            Remove-Item -LiteralPath $Full -Recurse -Force
        }
    }

    Write-Host "Running PyInstaller."
    & $Python -m PyInstaller --noconfirm --clean (Join-Path $Root "packaging\personal_assistant.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed (exit $LASTEXITCODE)."
    }

    $Output = Join-Path $Root "dist\LocalPersonalAssistant"
    $GuiExe = Join-Path $Output "LocalPersonalAssistant.exe"
    $CliExe = Join-Path $Output "LocalPersonalAssistantCLI.exe"
    if (-not (Test-Path $GuiExe) -or -not (Test-Path $CliExe)) {
        throw "The expected executables were not produced."
    }

    Copy-Item -LiteralPath (Join-Path $Root "config\config.example.yaml") -Destination (Join-Path $Output "config\config.example.yaml") -Force -ErrorAction SilentlyContinue
    $ModelOutput = Join-Path $Output "models"
    New-Item -ItemType Directory -Force -Path $ModelOutput | Out-Null
    Get-ChildItem -LiteralPath (Join-Path $Root "models") -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "intent_*" } |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $ModelOutput $_.Name) -Force }
    if (Test-Path (Join-Path $Root "docs")) {
        Copy-Item -LiteralPath (Join-Path $Root "docs") -Destination $Output -Recurse -Force
    }
    if (Test-Path (Join-Path $Root "README.md")) {
        Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination (Join-Path $Output "README.md") -Force
    }
    $VoiceScriptDir = Join-Path $Output "scripts"
    New-Item -ItemType Directory -Force -Path $VoiceScriptDir | Out-Null
    if (Test-Path (Join-Path $Root "scripts\install_voice_model.py")) {
        Copy-Item -LiteralPath (Join-Path $Root "scripts\install_voice_model.py") -Destination (Join-Path $VoiceScriptDir "install_voice_model.py") -Force
    }

    $PortableNote = @"
Local Personal Assistant — packaged build
Desktop: LocalPersonalAssistant.exe
Console: LocalPersonalAssistantCLI.exe --cli
Diagnostics: LocalPersonalAssistantCLI.exe --diagnose

Runtime data (memory database, logs, audit trail, recovery journal, temporary
files, and the intent model when retrained) is stored under:
  %LOCALAPPDATA%\PersonalAssistant

The bundled intent model is copied into the per-user data directory on first
run if no user-trained model exists. It is a small from-scratch classifier and
is used only as a label suggestion; it never executes actions.

The optional Vosk and Whisper speech models are not bundled. To use local
general speech or the 'hearty' wake word, install them once from a source
checkout or with a Python interpreter:
  python scripts\install_voice_model.py
  python scripts\install_whisper_model.py
Then choose speech.asr_engine: vosk or whisper in config\config.yaml. Wake
detection always uses Vosk; wake_command_engine: whisper is the default command
recognizer. No model is downloaded by the running assistant, and audio never
leaves this PC.

No personal data is included in this build. The application is offline-first;
the Python network guard covers Python sockets only, not child processes or
native libraries. See docs\SECURITY.md and docs\CAPABILITIES.md.
"@
    Set-Content -LiteralPath (Join-Path $Output "README_PORTABLE.txt") -Value $PortableNote -Encoding utf8

    if ($Zip) {
        $ZipPath = Join-Path $Root "dist\LocalPersonalAssistant-Packaged.zip"
        if (Test-Path -LiteralPath $ZipPath) {
            Remove-Item -LiteralPath $ZipPath -Force
        }
        Compress-Archive -LiteralPath $Output -DestinationPath $ZipPath -CompressionLevel Optimal
        Write-Host "Packaged zip: $ZipPath"
    }

    Write-Host ""
    Write-Host "Packaged build created:"
    Write-Host "  $Output"
    Write-Host "Desktop executable: $GuiExe"
    Write-Host "Console executable: $CliExe"
} finally {
    Pop-Location
}
