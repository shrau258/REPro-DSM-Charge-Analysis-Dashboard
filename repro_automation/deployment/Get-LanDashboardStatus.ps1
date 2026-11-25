[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$DashboardUrl = 'http://192.168.41.199:8080',
    [ValidateRange(1, 200)][int]$LogLines = 40
)

$scriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($scriptPath)) { $scriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($scriptPath)) { throw 'Cannot determine this script path. Run the .ps1 file directly.' }
$scriptDirectory = Split-Path -Parent $scriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $scriptDirectory }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path

foreach ($name in @('postgresql-x64-18','EnrichDSMDashboard')) {
    $service = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $service) { Write-Output ("SERVICE {0}: not installed" -f $name); continue }
    $config = Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f $name) -ErrorAction SilentlyContinue
    Write-Output ("SERVICE {0}: status={1}; start={2}; account={3}; dependency={4}" -f $name,$service.Status,$service.StartType,$config.StartName,($config.Dependencies -join ','))
}

try {
    $response = Invoke-WebRequest -Uri $DashboardUrl -TimeoutSec 5 -UseBasicParsing
    Write-Output ("DASHBOARD {0}: HTTP {1}" -f $DashboardUrl,$response.StatusCode)
} catch {
    Write-Output ("DASHBOARD {0}: unavailable ({1})" -f $DashboardUrl,$_.Exception.Message)
}
try {
    $health = Invoke-RestMethod -Uri ($DashboardUrl.TrimEnd('/') + '/api/health') -TimeoutSec 5
    Write-Output ("POSTGRES HEALTH: {0}; database={1}" -f $health.status,$health.database)
} catch {
    Write-Output ("POSTGRES HEALTH: unavailable ({0})" -f $_.Exception.Message)
}

foreach ($logName in @('service-supervisor.log','uvicorn-error.log','lan-service.log','lan-service-error.log','download_errors.log')) {
    if ($logName -eq 'download_errors.log') { $relativePath = $logName } else { $relativePath = "logs\$logName" }
    $path = Join-Path $ProjectRoot $relativePath
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Write-Output "--- Recent $logName ($path) ---"
        Get-Content -LiteralPath $path -Tail $LogLines
    }
}
