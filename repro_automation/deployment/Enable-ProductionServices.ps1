[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
foreach ($name in @('EnrichDSMApi', 'EnrichDSMCaddy')) {
    $service = Get-Service -Name $name -ErrorAction Stop
    if ($service.Status -ne 'Stopped') { Stop-Service -Name $name -Force }
    Set-Service -Name $name -StartupType Automatic
}
Start-Service -Name 'EnrichDSMApi'
$health = $null
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/health' -TimeoutSec 3
        if ($health.status -eq 'ok') { break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $health -or $health.status -ne 'ok') { throw 'FastAPI/PostgreSQL health check failed. Inspect logs/api-error.log before starting Caddy.' }
Start-Service -Name 'EnrichDSMCaddy'
Write-Host 'API and Caddy services are running. Confirm HTTPS reachability using the IT-provided hostname.'
