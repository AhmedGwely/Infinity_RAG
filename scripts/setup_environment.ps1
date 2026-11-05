param(
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvironmentPath = Join-Path $ProjectRoot "RAG_env"
$PythonExecutable = Join-Path $EnvironmentPath "Scripts\python.exe"

if (-not (Test-Path $PythonExecutable)) {
    Write-Host "Creating Python 3.12 virtual environment at $EnvironmentPath"
    python -m venv $EnvironmentPath
}

if ($InstallDependencies) {
    Write-Host "Installing declared project dependencies. This may download large packages."
    & $PythonExecutable -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
} else {
    Write-Host "Environment is ready. Dependencies were not installed."
    Write-Host "To install them explicitly, run: .\scripts\setup_environment.ps1 -InstallDependencies"
}

Write-Host "Activate with: .\RAG_env\Scripts\Activate.ps1"
