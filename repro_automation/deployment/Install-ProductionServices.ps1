[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [Parameter(Mandatory = $true)][string]$NssmPath,
    [Parameter(Mandatory = $true)][string]$CaddyPath,
    [Parameter(Mandatory = $true)][string]$DashboardHost,
    [Parameter(Mandatory = $true)][string]$ServerLanIp,
    [Parameter(Mandatory = $true)][string]$TrustedGatewayIps,
    [Parameter(Mandatory = $true)][string]$TlsCertificatePath,
    [Parameter(Mandatory = $true)][string]$TlsPrivateKeyPath
)

$ErrorActionPreference = 'Stop'
$ApiService = 'EnrichDSMApi'
$WebService = 'EnrichDSMCaddy'

function ConvertTo-CaddyPath([string]$Value) {
    $resolved = [System.IO.Path]::GetFullPath($Value).Replace('\', '/')
    return '"' + $resolved.Replace('"', '\"') + '"'
}

function Read-ProjectEnvValue([string]$Key) {
    $line = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

function Invoke-Nssm([string[]]$NssmArguments) {
    & $NssmPath @NssmArguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "NSSM command failed while configuring $($NssmArguments[0])." }
}

function Set-NssmService([string]$Name, [string]$Application, [string]$Arguments, [string]$WorkingDirectory, [string]$Stdout, [string]$Stderr) {
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne 'Stopped') { Stop-Service -Name $Name -Force }
        Invoke-Nssm @('set', $Name, 'Application', $Application)
    } else {
        Invoke-Nssm @('install', $Name, $Application)
    }
    Invoke-Nssm @('set', $Name, 'AppDirectory', $WorkingDirectory)
    Invoke-Nssm @('set', $Name, 'AppParameters', $Arguments)
    Invoke-Nssm @('set', $Name, 'AppStdout', $Stdout)
    Invoke-Nssm @('set', $Name, 'AppStderr', $Stderr)
    Invoke-Nssm @('set', $Name, 'AppRotateFiles', '1')
    Invoke-Nssm @('set', $Name, 'AppRotateOnline', '1')
    Invoke-Nssm @('set', $Name, 'AppRotateBytes', '10485760')
    Invoke-Nssm @('set', $Name, 'AppExit', 'Default', 'Restart')
    Invoke-Nssm @('set', $Name, 'AppRestartDelay', '5000')
    Invoke-Nssm @('set', $Name, 'AppKillProcessTree', '1')
    # Leave disabled until IT assigns its least-privilege service identity and
    # grants that identity the required filesystem/certificate access.
    Invoke-Nssm @('set', $Name, 'Start', 'SERVICE_DISABLED')
}

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw 'ProjectRoot does not exist.' }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env') -PathType Leaf)) { throw 'Project .env is missing. Create it from deployment/production.env.example and configure secrets privately.' }
$dbBackend = Read-ProjectEnvValue 'REPRO_DB_BACKEND'
$authEnabled = Read-ProjectEnvValue 'REPRO_AUTH_REQUIRED'
$proxyPeers = (Read-ProjectEnvValue 'REPRO_TRUSTED_PROXY_IPS') -split ',' | ForEach-Object { $_.Trim() }
$dashboardOrigins = (Read-ProjectEnvValue 'REPRO_CORS_ORIGINS') -split ',' | ForEach-Object { $_.Trim().TrimEnd('/') }
if ($dbBackend -ne 'postgres') { throw 'Production requires REPRO_DB_BACKEND=postgres.' }
if ($authEnabled -ne 'true') { throw 'Production requires REPRO_AUTH_REQUIRED=true. Do not expose an unauthenticated dashboard.' }
if ($proxyPeers -notcontains '127.0.0.1') { throw 'REPRO_TRUSTED_PROXY_IPS must include 127.0.0.1 because local Caddy is the FastAPI peer.' }
if ($dashboardOrigins -notcontains "https://$DashboardHost") { throw "REPRO_CORS_ORIGINS must include https://$DashboardHost for protected POST requests." }
if (-not (Read-ProjectEnvValue 'REPRO_AUTH_USER_HEADER') -or -not (Read-ProjectEnvValue 'REPRO_AUTH_ROLES_HEADER') -or -not (Read-ProjectEnvValue 'REPRO_ADMIN_ROLE') -or -not (Read-ProjectEnvValue 'REPRO_AUTOMATION_ROLE')) { throw 'Configure the authenticated user/roles headers and required roles in .env.' }
$requiredEnvKeys = @('REPRO_PG_HOST','REPRO_PG_PORT','REPRO_PG_DATABASE','REPRO_PG_USER','REPRO_PG_PASSWORD','REPRO_USERNAME','REPRO_PASSWORD')
foreach ($key in $requiredEnvKeys) {
    $value = Read-ProjectEnvValue $key
    if (-not $value -or $value -eq 'SET_BY_IT') { throw "Configure $key in the private .env." }
}
$browserPath = Read-ProjectEnvValue 'PLAYWRIGHT_BROWSERS_PATH'
if (-not $browserPath -or $browserPath -eq 'SET_BY_IT' -or -not [System.IO.Path]::IsPathRooted($browserPath) -or -not (Test-Path -LiteralPath $browserPath -PathType Container)) { throw 'Configure and install Chromium in an absolute shared PLAYWRIGHT_BROWSERS_PATH directory.' }
$NssmPath = (Resolve-Path -LiteralPath $NssmPath).Path
$CaddyPath = (Resolve-Path -LiteralPath $CaddyPath).Path
$TlsCertificatePath = (Resolve-Path -LiteralPath $TlsCertificatePath).Path
$TlsPrivateKeyPath = (Resolve-Path -LiteralPath $TlsPrivateKeyPath).Path
$PythonPath = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$WebRoot = Join-Path $ProjectRoot 'frontend\dist'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Project .venv is missing; install Python dependencies before installing services.' }
if (-not (Test-Path -LiteralPath (Join-Path $WebRoot 'index.html') -PathType Leaf)) { throw 'Production frontend build is missing; run npm ci and npm run build in frontend first.' }
if (-not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) { throw 'NSSM executable was not found.' }
if (-not (Test-Path -LiteralPath $CaddyPath -PathType Leaf)) { throw 'Caddy executable was not found.' }
$dashboardIp = $null
$dashboardIsIp = [System.Net.IPAddress]::TryParse($DashboardHost, [ref]$dashboardIp)
if ($dashboardIsIp) {
    if ($dashboardIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'For IP-based dashboard access, DashboardHost must be an IPv4 address.' }
    $DashboardHost = $dashboardIp.ToString()
} elseif ($DashboardHost -notmatch '^(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$') {
    throw 'DashboardHost must be an IT-provided DNS hostname or IPv4 address.'
}
$parsedIp = $null
if (-not [System.Net.IPAddress]::TryParse($ServerLanIp, [ref]$parsedIp)) { throw 'ServerLanIp must be a static IP address configured by IT.' }
if ($dashboardIsIp -and $DashboardHost -ne $parsedIp.ToString()) { throw 'For IP-based access, DashboardHost must match ServerLanIp so the HTTPS certificate and Caddy listener identify the same server address.' }
$gatewayEntries = @($TrustedGatewayIps -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $gatewayEntries.Count) { throw 'TrustedGatewayIps must contain the SSO gateway source IP address(es) or CIDR ranges supplied by IT.' }
foreach ($entry in $gatewayEntries) {
    $parts = $entry -split '/', 2
    $gatewayIp = $null
    if (-not [System.Net.IPAddress]::TryParse($parts[0], [ref]$gatewayIp)) { throw "Invalid trusted gateway address: $entry" }
    if ($parts.Count -eq 2) {
        $prefix = 0
        $maxPrefix = if ($gatewayIp.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) { 32 } else { 128 }
        if (-not [int]::TryParse($parts[1], [ref]$prefix) -or $prefix -lt 0 -or $prefix -gt $maxPrefix) { throw "Invalid trusted gateway CIDR prefix: $entry" }
    }
}

$Logs = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$TemplatePath = Join-Path $PSScriptRoot 'Caddyfile.template'
$CaddyfilePath = Join-Path $PSScriptRoot 'Caddyfile.production'
$template = Get-Content -LiteralPath $TemplatePath -Raw
$replacements = @{
    '{{DASHBOARD_HOST}}' = $DashboardHost
    '{{SERVER_LAN_IP}}' = $ServerLanIp
    '{{TRUSTED_GATEWAY_IPS}}' = ($gatewayEntries -join ' ')
    '{{TLS_CERT_PATH}}' = ConvertTo-CaddyPath $TlsCertificatePath
    '{{TLS_KEY_PATH}}' = ConvertTo-CaddyPath $TlsPrivateKeyPath
    '{{WEB_ROOT}}' = ConvertTo-CaddyPath $WebRoot
    '{{ACCESS_LOG}}' = ConvertTo-CaddyPath (Join-Path $Logs 'caddy-access.jsonl')
}
foreach ($key in $replacements.Keys) { $template = $template.Replace($key, $replacements[$key]) }
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($CaddyfilePath, $template, $utf8NoBom)
& $CaddyPath validate --config $CaddyfilePath --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'Caddy configuration validation failed; no service was started.' }

Set-NssmService $ApiService $PythonPath '-m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers --workers 1' `
    $ProjectRoot (Join-Path $Logs 'api-out.log') (Join-Path $Logs 'api-error.log')
$CaddyArguments = 'run --config "' + $CaddyfilePath + '" --adapter caddyfile'
Set-NssmService $WebService $CaddyPath $CaddyArguments $ProjectRoot `
    (Join-Path $Logs 'caddy-out.log') (Join-Path $Logs 'caddy-error.log')

Write-Host 'Production services installed and left disabled.'
Write-Host 'Assign the approved least-privilege service account to both services in services.msc.'
Write-Host 'Grant it read access to the project/.env and TLS private key, and Modify access only to downloads, uploads, artifacts, job_exports, and logs.'
Write-Host 'Then run deployment/Enable-ProductionServices.ps1 from an elevated PowerShell session.'
Write-Host 'No firewall, DNS, static IP, or PostgreSQL settings were changed.'
