[CmdletBinding()]
param(
    [string]$ListenAddress = '192.168.41.199',
    [ValidateRange(1024, 65535)][int]$Port = 8080,
    [string[]]$AdditionalAllowedCidrs = @('192.168.2.0/24'),
    [switch]$ServiceMode,
    [ValidateRange(5, 300)][int]$HealthCheckIntervalSeconds = 30,
    [ValidateRange(1, 10)][int]$HealthFailureLimit = 3,
    [ValidateRange(15, 600)][int]$StartupGraceSeconds = 120
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$indexFile = Join-Path $projectRoot 'frontend\dist\index.html'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python environment not found: $python"
}
if (-not (Test-Path -LiteralPath $indexFile -PathType Leaf)) {
    throw 'Built frontend is missing. Build frontend/dist before starting the LAN dashboard.'
}
$nodePath = $env:PLAYWRIGHT_NODEJS_PATH
if (-not $nodePath -or -not (Test-Path -LiteralPath $nodePath -PathType Leaf)) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($nodeCommand) { $nodePath = $nodeCommand.Source }
}
if (-not $nodePath -or -not (Test-Path -LiteralPath $nodePath -PathType Leaf)) {
    throw 'System Node.js was not found. Install the company-approved Node.js runtime so Playwright can start without executing its blocked bundled node.exe.'
}
$adapterAddress = [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces() |
    Where-Object { $_.OperationalStatus -eq [System.Net.NetworkInformation.OperationalStatus]::Up } |
    ForEach-Object { $_.GetIPProperties().UnicastAddresses } |
    Where-Object { $_.Address.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and $_.Address.IPAddressToString -eq $ListenAddress } |
    Select-Object -First 1
if (-not $adapterAddress) {
    throw "IP $ListenAddress is not currently assigned to this machine. Check ipconfig and rerun with -ListenAddress <this-machine-IPv4>."
}

# Derive the allowed LAN subnet from the selected adapter's address and prefix.
$octets = [System.Net.IPAddress]::Parse($ListenAddress).GetAddressBytes()
$remainingBits = [int]$adapterAddress.PrefixLength
$networkOctets = foreach ($octet in $octets) {
    if ($remainingBits -ge 8) {
        $networkOctet = [int]$octet
        $remainingBits -= 8
    } elseif ($remainingBits -gt 0) {
        $mask = (0xFF -shl (8 - $remainingBits)) -band 0xFF
        $networkOctet = ([int]$octet) -band $mask
        $remainingBits = 0
    } else {
        $networkOctet = 0
    }
    [string]$networkOctet
}
$allowedCidr = ($networkOctets -join '.') + '/' + [string]$adapterAddress.PrefixLength
$additionalNetworks = @($AdditionalAllowedCidrs | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($network in $additionalNetworks) {
    $parts = $network -split '/', 2
    $networkIp = $null
    $prefixLength = 0
    if ($parts.Count -ne 2 -or -not [System.Net.IPAddress]::TryParse($parts[0], [ref]$networkIp)) {
        throw "Invalid AdditionalAllowedCidrs entry: $network"
    }
    $maxPrefix = if ($networkIp.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { 32 } else { 128 }
    if (-not [int]::TryParse($parts[1], [ref]$prefixLength) -or $prefixLength -lt 0 -or $prefixLength -gt $maxPrefix) {
        throw "Invalid prefix in AdditionalAllowedCidrs entry: $network"
    }
}
$allowedCidrs = @($allowedCidr) + $additionalNetworks | Select-Object -Unique

$listener = Get-NetTCPConnection -State Listen -LocalAddress $ListenAddress -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is already listening on $ListenAddress (PID $($listener[0].OwningProcess))." }

# This explicit LAN mode grants dashboard access to every device in the adapter subnet.
$env:REPRO_SERVE_FRONTEND = 'true'
$env:REPRO_FRONTEND_DIST = (Join-Path $projectRoot 'frontend\dist')
$env:REPRO_LAN_ALLOWED_CIDRS = $allowedCidrs -join ','
$env:REPRO_AUTH_REQUIRED = 'false'
$env:PLAYWRIGHT_NODEJS_PATH = $nodePath

Write-Host "RE.Pro DSM Dashboard: http://${ListenAddress}:$Port"
Write-Host "LAN access restriction: $($allowedCidrs -join ', ') (plus this machine's loopback)"
Write-Host "Playwright Node.js: $nodePath"
Write-Host 'Windows Firewall rules are not changed by this script.'

if (-not $ServiceMode) {
    Write-Host 'Keep this PowerShell window open. Press Ctrl+C to stop the dashboard.'
    & $python -m uvicorn backend.main:app --host $ListenAddress --port $Port --no-proxy-headers --workers 1
    if ($LASTEXITCODE -ne 0) { throw "Dashboard server exited with code $LASTEXITCODE." }
    exit 0
}

# NSSM supervises this PowerShell process. This in-process loop supervises the
# actual Uvicorn child, probes the FastAPI/PostgreSQL health endpoint and
# restarts only the child this script launched when it exits or stays unhealthy.
$logs = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$supervisorLog = Join-Path $logs 'service-supervisor.log'
$stdoutLog = Join-Path $logs 'uvicorn-out.log'
$stderrLog = Join-Path $logs 'uvicorn-error.log'

function Write-SupervisorLog([string]$Message) {
    $line = '[{0}] {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($supervisorLog, $line, [System.Text.Encoding]::UTF8)
    Write-Output $line.TrimEnd()
}

function Rotate-SupervisorLog([string]$Path, [switch]$Force) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    if (-not $Force -and (Get-Item -LiteralPath $Path).Length -lt 10MB) { return }
    for ($index = 4; $index -ge 1; $index--) {
        $source = if ($index -eq 1) { $Path } else { "$Path.$($index - 1)" }
        $destination = "$Path.$index"
        if (Test-Path -LiteralPath $source) {
            if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Force }
            Move-Item -LiteralPath $source -Destination $destination -Force
        }
    }
}

function Stop-ManagedUvicorn([System.Diagnostics.Process]$Process) {
    if ($Process.HasExited) { return }
    Write-SupervisorLog "Stopping managed Uvicorn PID $($Process.Id) and its child process tree."
    & taskkill.exe /PID $Process.Id /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and -not $Process.HasExited) {
        try { $Process.Kill() } catch {}
    }
    if (-not $Process.WaitForExit(15000)) { throw "Managed Uvicorn PID $($Process.Id) did not stop within 15 seconds." }
}

$restartCount = 0
$healthySince = $null
while ($true) {
    Rotate-SupervisorLog $supervisorLog
    Rotate-SupervisorLog $stdoutLog -Force
    Rotate-SupervisorLog $stderrLog -Force
    $process = $null
    $healthySince = $null
    try {
        $process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','backend.main:app','--host',$ListenAddress,'--port',[string]$Port,'--no-proxy-headers','--workers','1') `
            -WorkingDirectory $projectRoot -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Hidden -PassThru
        $startedAt = [DateTime]::UtcNow
        $nextHealthCheck = $startedAt.AddSeconds($StartupGraceSeconds)
        $unhealthyCount = 0
        Write-SupervisorLog "Started Uvicorn PID $($process.Id) on ${ListenAddress}:$Port; health checks every $HealthCheckIntervalSeconds seconds after ${StartupGraceSeconds}s startup grace."

        while (-not $process.HasExited) {
            if ([DateTime]::UtcNow -ge $nextHealthCheck) {
                $nextHealthCheck = [DateTime]::UtcNow.AddSeconds($HealthCheckIntervalSeconds)
                try {
                    $health = Invoke-RestMethod -Uri "http://${ListenAddress}:$Port/api/health" -TimeoutSec 5
                    if ($health.status -eq 'ok') {
                        if ($unhealthyCount -gt 0) { Write-SupervisorLog 'FastAPI/PostgreSQL health recovered.' }
                        $unhealthyCount = 0
                        if (-not $healthySince) { $healthySince = [DateTime]::UtcNow }
                        Write-SupervisorLog 'Health check passed (FastAPI and PostgreSQL).'
                    } else {
                        $unhealthyCount++
                        Write-SupervisorLog "Health endpoint returned status '$($health.status)' ($unhealthyCount/$HealthFailureLimit consecutive failures)."
                    }
                } catch {
                    $unhealthyCount++
                    Write-SupervisorLog "Health check failed ($unhealthyCount/$HealthFailureLimit): $($_.Exception.Message)"
                }
                if ($unhealthyCount -ge $HealthFailureLimit) {
                    Stop-ManagedUvicorn $process
                    break
                }
            }
            Start-Sleep -Seconds 1
            $process.Refresh()
        }

        if ($process.HasExited) {
            Write-SupervisorLog "Uvicorn PID $($process.Id) exited with code $($process.ExitCode); scheduling a managed restart."
        } else {
            Write-SupervisorLog "Uvicorn PID $($process.Id) was restarted after repeated health failures."
        }
        if ($healthySince -and ([DateTime]::UtcNow - $healthySince).TotalMinutes -ge 10) { $restartCount = 0 }
        $restartCount++
        $restartDelay = [Math]::Min(60, [int](5 * [Math]::Pow(2, [Math]::Min(4, $restartCount - 1))))
        if ($restartCount -ge 5) {
            Write-SupervisorLog 'Five or more consecutive restarts observed; applying a 60-second circuit-breaker delay before retrying.'
            $restartDelay = 60
        }
        Write-SupervisorLog "Restarting managed Uvicorn after ${restartDelay}s backoff."
        Start-Sleep -Seconds $restartDelay
    } catch {
        Write-SupervisorLog "Supervisor error: $($_.Exception.Message)"
        if ($process -and -not $process.HasExited) { Stop-ManagedUvicorn $process }
        Start-Sleep -Seconds 30
    } finally {
        if ($process) { $process.Dispose() }
    }
}
