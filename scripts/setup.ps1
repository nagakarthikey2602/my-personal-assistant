<#
.SYNOPSIS
    Create or refresh the local Python environment for the assistant.

.DESCRIPTION
    This is the only script that may need network access (when pip must download
    packages). The assistant itself has no runtime download path. Pass -Offline
    with a local wheelhouse to install without network access.

.PARAMETER Python
    Base Python executable or command to use for the virtual environment.

.PARAMETER Offline
    Install only from -Wheelhouse using pip --no-index.

.PARAMETER Wheelhouse
    Folder containing wheels for offline installation.

.PARAMETER Dev
    Also install pytest and the optional PyInstaller build tool.

.PARAMETER NoConfig
    Do not create config/config.yaml from the reviewed example.

.PARAMETER Test
    Run the automated test suite after installation.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$Offline,
    [string]$Wheelhouse = "",
    [switch]$Dev,
    [switch]$NoConfig,
    [switch]$Test
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "Local Personal Assistant setup"
Write-Host "Project root: $Root"

if (-not (Test-Path $VenvPython)) {
    if ($Python) {
        $BasePython = $Python
    } else {
        $Command = Get-Command python -ErrorAction SilentlyContinue
        if (-not $Command) {
            throw "Python was not found on PATH. Install CPython 3.11-3.14, then rerun with -Python <path-to-python.exe>."
        }
        $BasePython = $Command.Source
    }
    Write-Host "Creating .venv with $BasePython"
    & $BasePython -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the virtual environment (exit $LASTEXITCODE)."
    }
} else {
    Write-Host "Using existing virtual environment: $VenvPython"
}

$Version = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
Write-Host "Python version: $Version"

if (-not $Offline) {
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upgrade pip (exit $LASTEXITCODE). Rerun with -Offline and a wheelhouse if the network is unavailable."
    }
    if ($Dev) {
        & $VenvPython -m pip install -r (Join-Path $Root "requirements-dev.txt")
    } else {
        & $VenvPython -m pip install -r (Join-Path $Root "requirements.txt")
    }
} else {
    if (-not $Wheelhouse) {
        $Wheelhouse = Join-Path $Root "wheels"
    }
    if (-not (Test-Path $Wheelhouse)) {
        throw "Offline wheelhouse not found: $Wheelhouse. Place wheels there or omit -Offline."
    }
    $Requirements = if ($Dev) { "requirements-dev.txt" } else { "requirements.txt" }
    Write-Host "Installing offline from $Wheelhouse"
    & $VenvPython -m pip install --no-index --find-links $Wheelhouse -r (Join-Path $Root $Requirements)
}
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed (exit $LASTEXITCODE)."
}

if (-not $NoConfig) {
    $Config = Join-Path $Root "config\config.yaml"
    if (-not (Test-Path $Config)) {
        Copy-Item -LiteralPath (Join-Path $Root "config\config.example.yaml") -Destination $Config
        Write-Host "Created config/config.yaml from the reviewed example."
    } else {
        Write-Host "Kept existing config/config.yaml."
    }
}

foreach ($Directory in @("sandbox", "data", "logs", "models", "training_data")) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Root $Directory) | Out-Null
}

Write-Host "Compiling the application modules (syntax check only)."
& $VenvPython -m compileall -q (Join-Path $Root "app")
if ($LASTEXITCODE -ne 0) {
    throw "Python compile check failed (exit $LASTEXITCODE)."
}

if ($Test) {
    Write-Host "Running the automated test suite."
    & $VenvPython -m pytest -q (Join-Path $Root "tests")
    if ($LASTEXITCODE -ne 0) {
        throw "Automated tests failed (exit $LASTEXITCODE)."
    }
}

Write-Host ""
Write-Host "Setup complete. Launch with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\launch.ps1"
Write-Host "Or diagnose with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File .\scripts\diagnose.ps1"
