<#
.SYNOPSIS  Diagnose (and, with -Fix, repair) the PostgreSQL login used by the INFINITY AI platform.
.DESCRIPTION
  PostgreSQL stores the role password INSIDE its data volume (infinity_pgdata) and only reads POSTGRES_PASSWORD when that
  volume is first created. If .env later holds a different value, every container still *shows* matching environment
  variables, yet the app is rejected with "password authentication failed". This script:
    1. reports what .env contains (length / risky characters only - the password is never printed),
    2. tests REAL TCP password authentication from a one-off API container using the effective runtime credentials
       (psql inside the postgres container uses the local socket, which is "trust" and proves nothing),
    3. with -Fix: sets the role password inside PostgreSQL to the value from .env (ALTER ROLE only - no data is touched,
       no volume is deleted), sending it through stdin so it never appears in a command line, log or screen,
    4. re-tests TCP authentication and runs `alembic current`.
  It never runs `down -v`, never recreates the database and never modifies .env.
.PARAMETER Fix  Apply the role-password repair when authentication fails.
#>
param([switch]$Fix)
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
$envPath = Join-Path (Get-Location) ".env"
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Bad($m)  { Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Info($m) { Write-Host "         $m" }

Step "1. .env"
if (-not (Test-Path $envPath)) { Bad ".env not found in $((Get-Location).Path). Copy the .env of the folder that created the database."; exit 1 }
$pw = $null
foreach ($line in Get-Content $envPath) {
  if ($line -match '^\s*POSTGRES_PASSWORD\s*=\s*(.*)$') { $raw = $Matches[1].Trim(); $pw = $raw; if ($pw.Length -ge 2 -and (($pw[0] -eq '"' -and $pw[-1] -eq '"') -or ($pw[0] -eq "'" -and $pw[-1] -eq "'"))) { $pw = $pw.Substring(1, $pw.Length - 2) } }
}
if (-not $pw) { Bad "POSTGRES_PASSWORD is missing in .env"; exit 1 }
Ok "POSTGRES_PASSWORD present, length $($pw.Length)"
if ($pw -match '\$') { Info "WARNING: contains '$' - Docker Compose expands it in unquoted/double-quoted .env values. Wrap the value in single quotes." }
if ($pw -match '\s|"|#') { Info "WARNING: contains whitespace, a double quote or '#' - quote the value in .env with single quotes." }

Step "2. Containers and volume"
docker volume inspect infinity_pgdata *> $null
if ($LASTEXITCODE -eq 0) { Ok "volume infinity_pgdata exists (it keeps the password it was created with)" } else { Info "volume infinity_pgdata not found - a fresh start will initialise it from .env, nothing to repair." }
docker compose up -d postgres redis | Out-Null
for ($i = 0; $i -lt 30; $i++) { $h = docker inspect -f "{{.State.Health.Status}}" (docker compose ps -q postgres) 2>$null; if ($h -eq "healthy") { break }; Start-Sleep 2 }
if ($h -ne "healthy") { Bad "postgres is not healthy: $h"; docker compose logs --tail 30 postgres; exit 1 }
Ok "postgres healthy"

function Test-Tcp {
  # One-off container, same image/env as the real API, real TCP connection through the compose network.
  docker compose run --rm --no-deps -T api python -m infinity.dbcheck --wait 30
  return $LASTEXITCODE
}

Step "3. TCP password authentication from an API container"
$rc = Test-Tcp
if ($rc -eq 0) { Ok "authentication works" }
elseif ($rc -eq 3) {
  Bad "PostgreSQL rejected the password from .env"
  if (-not $Fix) { Info "Re-run with -Fix to set the role password to the .env value (data is kept):  .\scripts\db-doctor.ps1 -Fix"; exit 3 }
  Step "3b. Repairing the role password (ALTER ROLE only; password sent via stdin, never printed)"
  $OutputEncoding = New-Object System.Text.UTF8Encoding $false
  $sql = "ALTER ROLE infinity WITH PASSWORD '" + ($pw -replace "'", "''") + "';"
  $sql | docker compose exec -T postgres psql -U infinity -d postgres -X -q -v ON_ERROR_STOP=1
  if ($LASTEXITCODE -ne 0) { Bad "ALTER ROLE failed"; exit 1 }
  $sql = $null
  Ok "role password updated"
  Step "3c. Re-testing TCP authentication"
  $rc = Test-Tcp
  if ($rc -ne 0) { Bad "still failing (exit $rc) - see the message above"; exit $rc }
  Ok "authentication works"
}
elseif ($rc -eq 2) { Bad "PostgreSQL not reachable from the API container"; exit 2 }
else { Bad "dbcheck failed (exit $rc). If it says 'No module named infinity.dbcheck', rebuild first:  docker compose build api"; exit $rc }

Step "4. Alembic"
docker compose run --rm --no-deps -T api alembic current
if ($LASTEXITCODE -ne 0) { Bad "alembic current failed"; exit 1 }
Ok "alembic can read the database"
Write-Host "`nNext:  docker compose up -d --build --force-recreate api worker web   (recreate, don't just restart: restart keeps the old environment)" -ForegroundColor Yellow
