[CmdletBinding()]
param(
    [string]$ListenAddress = '192.168.41.199',
    [ValidateRange(1024, 65535)][int]$Port = 8080,
    [string[]]$AdditionalAllowedCidrs = @('192.168.2.0/24')
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
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not (Test-Path -LiteralPath $nodeCommand.Source -PathType Leaf)) {
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
$env:PLAYWRIGHT_NODEJS_PATH = $nodeCommand.Source

Write-Host "RE.Pro DSM Dashboard: http://${ListenAddress}:$Port"
Write-Host "LAN access restriction: $($allowedCidrs -join ', ') (plus this machine's loopback)"
Write-Host "Playwright Node.js: $($nodeCommand.Source)"
Write-Host 'Keep this PowerShell window open. Press Ctrl+C to stop the dashboard.'
Write-Host 'Windows Firewall rules are not changed by this script.'
& $python -m uvicorn backend.main:app --host $ListenAddress --port $Port --no-proxy-headers --workers 1
if ($LASTEXITCODE -ne 0) { throw "Dashboard server exited with code $LASTEXITCODE." }
