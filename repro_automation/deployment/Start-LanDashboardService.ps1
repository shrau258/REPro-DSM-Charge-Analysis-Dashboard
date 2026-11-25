[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$ListenAddress = '192.168.41.199',
    [ValidateRange(1024, 65535)][int]$Port = 8080,
    [string[]]$AllowedCidrs = @('192.168.2.0/24', '192.168.41.0/24'),
    [ValidateRange(30, 1800)][int]$ReadinessTimeoutSeconds = 900
)

$deploymentScriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { $deploymentScriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { throw 'Cannot determine this deployment script path. Run the .ps1 file directly.' }
$deploymentScriptDirectory = Split-Path -Parent $deploymentScriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $deploymentScriptDirectory }

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$launcher = Join-Path $ProjectRoot 'start_lan_dashboard.ps1'
$preflightScript = Join-Path $deploymentScriptDirectory 'Test-LanDashboardAutomation.ps1'
$envFile = Join-Path $ProjectRoot '.env'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "LAN launcher not found: $launcher" }
if (-not (Test-Path -LiteralPath $preflightScript -PathType Leaf)) { throw "Automation preflight script not found: $preflightScript" }
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) { throw 'Project .env is missing.' }

# PowerShell -File binds one command-line value to an array parameter, while
# later bare values can accidentally bind to following parameters. Accept the
# installer-safe single CSV argument and normalize it before calling the LAN
# launcher. Preserve and explicitly validate the two approved LAN networks.
$AllowedCidrs = @($AllowedCidrs | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($AllowedCidrs.Count -ne 2 -or
    $AllowedCidrs -notcontains '192.168.2.0/24' -or
    $AllowedCidrs -notcontains '192.168.41.0/24') {
    throw 'AllowedCidrs must contain exactly 192.168.2.0/24 and 192.168.41.0/24.'
}

# Prevent a second service wrapper from attempting to bind the same dashboard.
$mutex = [System.Threading.Mutex]::new($false, 'Global\EnrichDSMDashboardLanService')
if (-not $mutex.WaitOne(0)) { throw 'Another RE.Pro LAN dashboard service process is already active.' }

function Read-EnvValue([string]$Key) {
    $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

function Test-TcpEndpoint([string]$HostName, [int]$TargetPort) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync($HostName, $TargetPort)
        return $task.Wait(2500) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

try {
    $pgHost = Read-EnvValue 'REPRO_PG_HOST'
    $pgPortText = Read-EnvValue 'REPRO_PG_PORT'
    $pgPort = 5432
    if ($pgPortText -and -not [int]::TryParse($pgPortText, [ref]$pgPort)) { throw 'REPRO_PG_PORT in .env must be an integer.' }
    if (-not $pgHost) { $pgHost = 'localhost' }

    $deadline = [DateTime]::UtcNow.AddSeconds($ReadinessTimeoutSeconds)
    Write-Output "Waiting for PostgreSQL at ${pgHost}:${pgPort} and dashboard IP $ListenAddress."
    while ([DateTime]::UtcNow -lt $deadline) {
        $ipReady = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
            Where-Object { $_.OperationalStatus -eq [System.Net.NetworkInformation.OperationalStatus]::Up } |
            ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
            Where-Object { $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $_.Address.IPAddressToString -eq $ListenAddress } |
            Select-Object -First 1
        if ($ipReady -and (Test-TcpEndpoint $pgHost $pgPort)) { break }
        Start-Sleep -Seconds 5
    }
    if (-not $ipReady -or -not (Test-TcpEndpoint $pgHost $pgPort)) {
        throw "PostgreSQL or dashboard IP was not ready within $ReadinessTimeoutSeconds seconds."
    }

    $expectedServiceAccount = 'NT SERVICE\EnrichDSMDashboard'
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ($currentIdentity -ine $expectedServiceAccount) {
        throw "Dashboard startup is running as '$currentIdentity'; expected '$expectedServiceAccount'. Refusing to run automation preflight or start the web app under an unexpected identity."
    }
    if ([string]::IsNullOrWhiteSpace($env:PLAYWRIGHT_BROWSERS_PATH)) {
        throw 'PLAYWRIGHT_BROWSERS_PATH is missing from the NSSM service environment.'
    }
    if ([string]::IsNullOrWhiteSpace($env:PLAYWRIGHT_NODEJS_PATH)) {
        throw 'PLAYWRIGHT_NODEJS_PATH is missing from the NSSM service environment.'
    }

    Write-Output "Running bounded Python/BAT/Chromium startup preflight in the service process as $currentIdentity."
    $env:PLAYWRIGHT_BROWSERS_PATH = (Resolve-Path -LiteralPath $env:PLAYWRIGHT_BROWSERS_PATH).Path
    $env:PLAYWRIGHT_NODEJS_PATH = (Resolve-Path -LiteralPath $env:PLAYWRIGHT_NODEJS_PATH).Path
    & $preflightScript -ProjectRoot $ProjectRoot -BrowserCachePath $env:PLAYWRIGHT_BROWSERS_PATH -ExpectedServiceAccount $expectedServiceAccount
    Write-Output 'Automation startup preflight passed; starting the production web application.'

    $listener = Get-NetTCPConnection -State Listen -LocalAddress $ListenAddress -LocalPort $Port -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port on $ListenAddress is already occupied by PID $($listener[0].OwningProcess); refusing to start a duplicate dashboard." }

    Write-Output "Starting existing LAN launcher as service identity '$([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)' at http://${ListenAddress}:$Port."
    & $launcher -ListenAddress $ListenAddress -Port $Port -AdditionalAllowedCidrs $AllowedCidrs -ServiceMode
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) { throw "LAN dashboard launcher exited with code $exitCode." }
    throw 'LAN dashboard launcher exited unexpectedly.'
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
