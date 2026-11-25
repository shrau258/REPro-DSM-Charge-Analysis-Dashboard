[CmdletBinding()]
param([string]$NssmPath = 'C:\nssm\nssm.exe\win64\ssm.exe')

$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator permissions are required. Open PowerShell with Run as administrator and run this script again.'
}
if (-not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) { throw "NSSM executable not found: $NssmPath" }
foreach ($name in @('EnrichDSMDashboardPreflight','EnrichDSMDashboard')) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
        & $NssmPath stop $name | Out-Null
        & $NssmPath remove $name confirm | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not remove Windows service '$name'." }
    }
}
Write-Output 'RE.Pro dashboard Windows service removed. Project files, downloads, PostgreSQL, and logs were preserved.'
Write-Output 'Manual fallback remains: .\start_lan_dashboard.ps1 -ListenAddress 192.168.41.199 -Port 8080 -AdditionalAllowedCidrs @(''192.168.2.0/24'',''192.168.41.0/24'')'
