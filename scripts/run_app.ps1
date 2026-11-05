$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ActivateScript = Join-Path $ProjectRoot "RAG_env\Scripts\Activate.ps1"

if (-not (Test-Path $ActivateScript)) {
    throw "RAG_env was not found. Run .\scripts\setup_environment.ps1 first."
}

. $ActivateScript
Set-Location $ProjectRoot
python -m uvicorn main:create_application --factory --reload
