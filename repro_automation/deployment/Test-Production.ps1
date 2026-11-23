[CmdletBinding()]
param([string]$DashboardUrl = '')
$ErrorActionPreference = 'Stop'
$api = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 10
if ($api.status -ne 'ok') { throw 'FastAPI did not report a healthy PostgreSQL connection.' }
$indexPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'frontend\dist\index.html'
if (-not (Test-Path -LiteralPath $indexPath) -or (Get-Content -LiteralPath $indexPath -Raw) -notmatch '<div id="root"></div>') {
    throw 'The production dashboard build is missing or invalid.'
}
Write-Host "FastAPI/PostgreSQL: $($api.status)"
Write-Host 'Dashboard production build: OK'
if ($DashboardUrl) {
    $page = Invoke-WebRequest -Uri $DashboardUrl -TimeoutSec 20 -UseBasicParsing
    if ($page.StatusCode -ne 200 -or $page.Content -notmatch '<div id="root"></div>') { throw 'Dashboard URL did not return the expected application HTML.' }
    Write-Host "Dashboard URL: HTTP $($page.StatusCode)"
}
Write-Host 'Test sign-in and authenticated APIs from a second LAN computer using the company SSO gateway.'
