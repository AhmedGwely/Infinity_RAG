<#
.SYNOPSIS  Start (or update) the INFINITY AI platform with Docker Compose.
.DESCRIPTION
  1. Verifies Docker, then INSPECTS (read-only) your existing Qdrant container and the rd_engineering collection.
  2. Creates .env if missing, or APPENDS only missing platform settings - existing values are never overwritten.
  3. Picks a free web port, builds and starts the stack, waits until it is healthy, and opens the browser.
  Nothing here deletes, recreates or re-indexes your Qdrant data.
.PARAMETER QdrantContainer  Name of your existing Qdrant container (default infinity-qdrant).
.PARAMETER NoBrowser        Do not open the browser when ready.
.PARAMETER SkipSnapshot     Do not take the one-time safety snapshot of the existing collection.
#>
param(
  [string]$QdrantContainer = "infinity-qdrant",
  [string]$Collection = "",
  [switch]$NoBrowser,
  [switch]$SkipSnapshot
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$envPath = Join-Path $PSScriptRoot ".env"

function Info($m) { Write-Host "  $m" }
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

function Read-EnvFile {
  $map = @{}
  if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $map[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
    }
  }
  return $map
}
function Add-EnvValues([hashtable]$values) {
  $existing = ""
  if (Test-Path $envPath) { $existing = [System.IO.File]::ReadAllText($envPath) }
  $sb = New-Object System.Text.StringBuilder
  if ($existing.Length -gt 0 -and -not $existing.EndsWith("`n")) { [void]$sb.Append("`n") }
  foreach ($k in $values.Keys) { [void]$sb.Append("$k=$($values[$k])`n") }
  [System.IO.File]::AppendAllText($envPath, $sb.ToString(), (New-Object System.Text.UTF8Encoding $false))
}
function New-RandomString([int]$length, [string]$alphabet) {
  $bytes = New-Object byte[] ($length * 2)
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create(); $rng.GetBytes($bytes); $rng.Dispose()
  $chars = for ($i = 0; $i -lt $length; $i++) { $alphabet[$bytes[$i] % $alphabet.Length] }
  return -join $chars
}
function New-StrongPassword {
  $u = New-RandomString 3 "ABCDEFGHJKLMNPQRSTUVWXYZ"; $l = New-RandomString 8 "abcdefghijkmnpqrstuvwxyz"; $d = New-RandomString 3 "23456789"
  $mix = ($u + $l + $d).ToCharArray() | Sort-Object { Get-Random }
  return -join $mix
}

# ---------------------------------------------------------------- 1. Docker
Step "Checking Docker"
try { docker info *> $null; if ($LASTEXITCODE -ne 0) { throw "daemon" } }
catch { Write-Host "Docker Desktop is not running. Start it, wait for 'Engine running', then run this script again." -ForegroundColor Red; exit 1 }
Info ("Docker " + (docker version --format '{{.Server.Version}}'))
Info ((docker compose version --short) -replace '^', 'Compose ')

# ---------------------------------------------------------------- 2. Inspect existing Qdrant (read-only)
Step "Inspecting your existing Qdrant container (read-only)"
$cfg = Read-EnvFile
if (-not $Collection) { $Collection = if ($cfg.ContainsKey("COLLECTION_NAME")) { $cfg["COLLECTION_NAME"] } else { "rd_engineering" } }
$qdrantUrlForHost = $null; $qdrantUrlForContainers = $null; $apiKey = $cfg["QDRANT_API_KEY"]
$found = docker ps -a --filter "name=^/$QdrantContainer$" --format "{{.Names}}"
if ($found -ne $QdrantContainer) {
  Warn "No container named '$QdrantContainer' was found."
  if ($cfg.ContainsKey("PLATFORM_QDRANT_URL")) { $qdrantUrlForContainers = $cfg["PLATFORM_QDRANT_URL"]; Info "Using PLATFORM_QDRANT_URL from .env: $qdrantUrlForContainers" }
  else { Warn "Set PLATFORM_QDRANT_URL in .env (e.g. http://host.docker.internal:6333), or start the bundled Qdrant: docker compose --profile bundled-qdrant up -d"; }
} else {
  $info = docker inspect $QdrantContainer | ConvertFrom-Json
  $c = @($info)[0]
  Info "Container : $($c.Name.TrimStart('/'))   image: $($c.Config.Image)   state: $($c.State.Status)"
  Info "Restart   : $($c.HostConfig.RestartPolicy.Name)"
  foreach ($m in $c.Mounts) { Info "Storage   : $($m.Type) $($m.Name) -> $($m.Destination)" }
  if ($c.State.Status -ne "running") { Warn "The Qdrant container is not running. Start it first:  docker start $QdrantContainer"; exit 1 }
  if ($c.HostConfig.RestartPolicy.Name -in @("", "no")) {
    Warn "Your Qdrant container has no restart policy, so it will NOT come back after a reboot."
    Warn "To fix (changes only its restart setting, not its data):  docker update --restart unless-stopped $QdrantContainer"
  }
  $binding = $c.NetworkSettings.Ports.'6333/tcp'
  if ($binding -and $binding.Count -gt 0) {
    $hostPort = $binding[0].HostPort
    $qdrantUrlForHost = "http://localhost:$hostPort"
    $qdrantUrlForContainers = "http://host.docker.internal:$hostPort"
    Info "REST port : host $hostPort -> container 6333"
  } else { Warn "Port 6333 is not published to the host; the platform cannot reach this Qdrant. Publish it (-p 6333:6333) or set PLATFORM_QDRANT_URL." }
  $envKey = ($c.Config.Env | Where-Object { $_ -like "QDRANT__SERVICE__API_KEY=*" })
  if ($envKey -and -not $apiKey) { Warn "This Qdrant requires an API key. Add QDRANT_API_KEY=<key> to .env and run again."; exit 1 }
}
$expectedPoints = $null
if ($qdrantUrlForHost) {
  try {
    $headers = @{}; if ($apiKey) { $headers["api-key"] = $apiKey }
    $col = Invoke-RestMethod -Uri "$qdrantUrlForHost/collections/$Collection" -Headers $headers -TimeoutSec 10
    $expectedPoints = $col.result.points_count
    $size = $col.result.config.params.vectors.size
    Info "Collection: $Collection  points=$expectedPoints  vector size=$size  distance=$($col.result.config.params.vectors.distance)"
    if ($size -ne 1024) { Warn "Expected 1024-dimensional vectors (BGE-M3); the platform will refuse to use this collection."; }
    if (-not $SkipSnapshot -and -not (Test-Path (Join-Path $PSScriptRoot "backups\.snapshot-taken"))) {
      Info "Taking a one-time safety snapshot (non-destructive)..."
      try {
        $snap = Invoke-RestMethod -Method Post -Uri "$qdrantUrlForHost/collections/$Collection/snapshots" -Headers $headers -TimeoutSec 120
        New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "backups") | Out-Null
        Set-Content -Path (Join-Path $PSScriptRoot "backups\.snapshot-taken") -Value $snap.result.name
        Info "Snapshot created inside Qdrant: $($snap.result.name)  (backup.ps1 can download it)"
      } catch { Warn "Snapshot skipped: $($_.Exception.Message)" }
    }
  } catch { Warn "Could not read collection '$Collection' ($($_.Exception.Message)). The platform will create it only if it is missing and never modifies an existing one." }
}

# ---------------------------------------------------------------- 3. .env (append-only)
Step "Preparing configuration (.env)"
$cfg = Read-EnvFile
$add = [ordered]@{}
$freshAdmin = $null
if (-not $cfg.ContainsKey("APP_SECRET_KEY")) { $add["APP_SECRET_KEY"] = New-RandomString 48 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789" }
if (-not $cfg.ContainsKey("POSTGRES_PASSWORD")) {
  # PostgreSQL stores the role password inside its data volume (fixed name: infinity_pgdata) and only reads POSTGRES_PASSWORD
  # when that volume is first created. If the volume already exists but this folder has no password in .env (e.g. the project
  # was copied/unzipped to a new folder), generating a new one would lock the app out of its own database.
  $existingVolumes = @(docker volume ls --format "{{.Name}}")
  $volumeExists = $existingVolumes -contains "infinity_pgdata"
  if ($volumeExists) {
    Write-Host "The PostgreSQL volume 'infinity_pgdata' already exists but .env has no POSTGRES_PASSWORD." -ForegroundColor Red
    Write-Host "Generating a new password would not match the stored one. Either copy POSTGRES_PASSWORD from the .env of the folder that created the volume," -ForegroundColor Red
    Write-Host "or set the password you want and run:  .\scripts\db-doctor.ps1 -Fix   (changes only the role password; all data is kept)." -ForegroundColor Red
    exit 1
  }
  $add["POSTGRES_PASSWORD"] = New-RandomString 28 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
}
if (-not $cfg.ContainsKey("BOOTSTRAP_ADMIN_PASSWORD")) { $freshAdmin = New-StrongPassword; $add["BOOTSTRAP_ADMIN_PASSWORD"] = $freshAdmin }
if (-not $cfg.ContainsKey("BOOTSTRAP_ADMIN_EMAIL")) { $add["BOOTSTRAP_ADMIN_EMAIL"] = "admin@infinity.local" }
if ($qdrantUrlForContainers -and -not $cfg.ContainsKey("PLATFORM_QDRANT_URL")) { $add["PLATFORM_QDRANT_URL"] = $qdrantUrlForContainers }
if (-not $cfg.ContainsKey("COLLECTION_NAME")) { $add["COLLECTION_NAME"] = $Collection }

# Free web port (default 8088). A port already used by the platform itself is fine.
$webPort = if ($cfg.ContainsKey("WEB_PORT")) { [int]$cfg["WEB_PORT"] } else { 8088 }
$ours = docker ps --filter "name=infinity-web" --format "{{.Ports}}"
function Test-PortBusy([int]$p) { return [bool](Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) }
if ((Test-PortBusy $webPort) -and -not ($ours -match ":$webPort->")) {
  $next = (($webPort + 1)..($webPort + 20) | Where-Object { -not (Test-PortBusy $_) } | Select-Object -First 1)
  if (-not $next) { Write-Host "No free port found near $webPort. Set WEB_PORT in .env." -ForegroundColor Red; exit 1 }
  Warn "Port $webPort is in use by another program; using $next instead."
  $webPort = $next; $add["WEB_PORT"] = "$next"
} elseif (-not $cfg.ContainsKey("WEB_PORT")) { $add["WEB_PORT"] = "$webPort" }

if ($add.Count -gt 0) { Add-EnvValues $add; Info ("Added to .env: " + ($add.Keys -join ", ")) } else { Info ".env already complete; nothing changed." }
$cfg = Read-EnvFile
if (-not $cfg.ContainsKey("GEMINI_API_KEY") -and -not $cfg.ContainsKey("OPENAI_API_KEY")) { Warn "No GEMINI_API_KEY in .env. Add one there, or enter a key later under Providers in the web interface." }

# ---------------------------------------------------------------- 4. Build & start
Step "Building and starting the platform (first build takes several minutes)"
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host "docker compose failed. Run 'docker compose logs --tail 100' to see why." -ForegroundColor Red; exit 1 }

Step "Waiting for the platform to become healthy"
$url = "http://localhost:$($cfg['WEB_PORT'])"
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
  try { $r = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 3; if ($r.status -eq "ok") { $ok = $true; break } } catch { }
  Start-Sleep -Seconds 2
}
if (-not $ok) { Warn "The web interface did not answer in time. Check:  docker compose ps   and   docker compose logs api"; exit 1 }

Write-Host "`nINFINITY AI is running:  $url" -ForegroundColor Green
if ($freshAdmin) {
  Write-Host "`n  First sign-in (shown once; also saved in .env):" -ForegroundColor Green
  Write-Host "    Email   : $($cfg['BOOTSTRAP_ADMIN_EMAIL'])"
  Write-Host "    Password: $freshAdmin"
  Write-Host "  Change it after signing in (account menu, bottom-left).`n"
}
Info "First start downloads the BGE-M3 embedding model (~2.3 GB). Until it is ready, keyword search works and"
Info "uploads wait in the queue. Watch progress under 'System & logs'."
Info "The existing Qdrant collection is mirrored into PostgreSQL automatically (read-only); no re-indexing."
if ($expectedPoints -ne $null) { Info "Expected after mirroring: $expectedPoints passages from the pre-existing collection." }
if (-not $NoBrowser) { Start-Process $url }
