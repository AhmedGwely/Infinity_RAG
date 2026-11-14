<#
.SYNOPSIS  Back up everything the platform owns: PostgreSQL, uploaded files, and a Qdrant snapshot.
.DESCRIPTION
  Creates backups\<timestamp>\ containing  postgres.sql, uploads.tar, qdrant-<collection>.snapshot (if reachable), and .env.
  Safe to run while the platform is running. The .env file contains secrets: keep the backup folder private.
#>
param([string]$Collection = "rd_engineering")
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dir = Join-Path $PSScriptRoot "backups\$stamp"
New-Item -ItemType Directory -Force -Path $dir | Out-Null

Write-Host "PostgreSQL..."
docker compose exec -T postgres pg_dump -U infinity -d infinity --clean --if-exists -f /tmp/pg.sql
docker compose cp postgres:/tmp/pg.sql (Join-Path $dir "postgres.sql")   # copied byte-for-byte (no PowerShell re-encoding)

Write-Host "Uploaded files..."
docker run --rm -v infinity_uploads:/data -v "${dir}:/out" alpine tar -cf /out/uploads.tar -C /data .

Write-Host "Qdrant snapshot..."
$cfg = @{}; Get-Content .env | ForEach-Object { if ($_ -match '^\s*([A-Za-z_]\w*)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() } }
if ($cfg["COLLECTION_NAME"]) { $Collection = $cfg["COLLECTION_NAME"] }
$hostUrl = ($cfg["PLATFORM_QDRANT_URL"] -replace "host\.docker\.internal", "localhost")
$headers = @{}; if ($cfg["QDRANT_API_KEY"]) { $headers["api-key"] = $cfg["QDRANT_API_KEY"] }
try {
  $snap = Invoke-RestMethod -Method Post -Uri "$hostUrl/collections/$Collection/snapshots" -Headers $headers -TimeoutSec 300
  Invoke-WebRequest -Uri "$hostUrl/collections/$Collection/snapshots/$($snap.result.name)" -Headers $headers -OutFile (Join-Path $dir "qdrant-$Collection.snapshot")
} catch { Write-Host "  Qdrant snapshot skipped: $($_.Exception.Message)" -ForegroundColor Yellow }

Copy-Item .env (Join-Path $dir "env.backup")
Write-Host "Backup written to $dir" -ForegroundColor Green
Write-Host "Restore notes: docs\OPERATIONS.md"
