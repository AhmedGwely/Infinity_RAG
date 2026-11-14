<#
.SYNOPSIS  Find the original APP_SECRET_KEY that encrypted the stored provider/source credentials (read-only, never prints keys).
.DESCRIPTION
  1. Backs up PostgreSQL (pg_dump) to backups\keyrecovery-<timestamp>\ BEFORE anything else.
  2. Reports whether the CURRENT key can decrypt each encrypted record (fingerprints only).
  3. Collects candidate APP_SECRET_KEY values from: .env / .env.* / *.env files under -Roots (this includes backups\<ts>\.env
     written by backup.ps1), and the environment of EVERY Docker container and image on this machine (including stopped/old ones).
  4. Pipes the candidates over stdin to `python -m infinity.keytool try-keys` inside the api container, which tests each one
     (as-is / trimmed / unquoted) with the application's own SecretBox against every encrypted record.
  Output shows only labels (file path or container name) and 12-hex fingerprints. The database is never modified.
  If a candidate MATCHES: put that key in .env as APP_SECRET_KEY (copy it from the labelled file), then
  `docker compose up -d --force-recreate api worker`. If nothing matches, the credentials are unrecoverable: re-enter them in the
  web interface (Providers page); the app now handles this safely.
.PARAMETER Roots  Folders to scan for .env files. Default: this project, its parent folder, and D:\Development.
#>
param([string[]]$Roots)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -Path $repo
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
if (-not $Roots) { $Roots = @($repo, (Split-Path -Parent $repo), "D:\Development") | Where-Object { Test-Path $_ } | Select-Object -Unique }

Step "1. Backup (pg_dump) before anything else"
$dir = Join-Path $repo ("backups\keyrecovery-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
docker compose exec -T postgres pg_dump -U infinity -d infinity --clean --if-exists -f /tmp/keyrecovery.sql
if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed - stopping; nothing was changed." -ForegroundColor Red; exit 1 }
docker compose cp postgres:/tmp/keyrecovery.sql (Join-Path $dir "postgres.sql") | Out-Null
Info ("backup: " + (Join-Path $dir "postgres.sql") + "  (" + [math]::Round((Get-Item (Join-Path $dir "postgres.sql")).Length / 1KB) + " KB)")

Step "2. Current key vs stored encrypted records"
docker compose exec -T api python -m infinity.keytool status

Step "3. Collecting candidate keys (values are never displayed)"
$cand = New-Object System.Collections.Generic.List[string]
$files = Get-ChildItem -Path $Roots -Recurse -Force -File -ErrorAction SilentlyContinue |
  Where-Object { ($_.Name -eq ".env" -or $_.Name -like ".env.*" -or $_.Name -like "*.env") -and $_.Length -lt 200KB -and $_.FullName -notmatch "\\(node_modules|\.git)\\" }
foreach ($f in $files) {
  try { foreach ($line in [System.IO.File]::ReadAllLines($f.FullName)) {
    if ($line -match '^\s*(export\s+)?APP_SECRET_KEY\s*=(.*)$') { $cand.Add("file $($f.FullName)`t$($Matches[2])") }
  } } catch { }
}
Info "env files with APP_SECRET_KEY: $($cand.Count) (from $(@($files).Count) env file(s) scanned)"
$n0 = $cand.Count
foreach ($id in (docker ps -aq)) {
  $name = (docker inspect -f "{{.Name}} [{{.State.Status}}]" $id)
  foreach ($e in (docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" $id)) {
    if ($e -like "APP_SECRET_KEY=*") { $cand.Add("container $name`t$($e.Substring(15))") }
  }
}
foreach ($img in (docker image ls -q | Select-Object -Unique)) {
  foreach ($e in (docker image inspect -f "{{range .Config.Env}}{{println .}}{{end}}" $img 2>$null)) {
    if ($e -like "APP_SECRET_KEY=*") { $cand.Add("image $img`t$($e.Substring(15))") }
  }
}
Info "from containers/images: $($cand.Count - $n0)"
if ($cand.Count -eq 0) { Write-Host "No candidate keys found anywhere." -ForegroundColor Yellow }

Step "4. Testing candidates against the encrypted records (read-only)"
($cand -join "`n") | docker compose exec -T api python -m infinity.keytool try-keys
$rc = $LASTEXITCODE
if ($rc -eq 0) {
  Write-Host "`nA candidate key decrypts the stored credentials. Copy that key (from the labelled file) into .env as APP_SECRET_KEY, then:" -ForegroundColor Green
  Write-Host "  docker compose up -d --force-recreate api worker" -ForegroundColor Green
} else {
  Write-Host "`nNo candidate decrypts the stored credentials. They cannot be recovered; the original encrypted record is kept untouched." -ForegroundColor Yellow
  Write-Host "Open the web interface > Providers, enter the API key and press Save. That replaces the unreadable value." -ForegroundColor Yellow
}
Write-Host "Backup kept in: $dir"
exit $rc
