# Stops the platform containers. All data (PostgreSQL, uploads, model cache) is kept. Your Qdrant container is untouched.
Set-Location -Path $PSScriptRoot
docker compose stop
Write-Host "Stopped. Start again with .\start.ps1 (or: docker compose up -d)."
