[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$NssmPath = 'C:\nssm\nssm.exe\win64\ssm.exe',
    [string]$ListenAddress = '192.168.41.199',
    [ValidateRange(1024, 65535)][int]$Port = 8080,
    [string]$PostgresServiceName = 'postgresql-x64-18',
    [string]$MasterExcelFolder = '',
    [string]$BrowserCachePath = '',
    [ValidateRange(5, 120)][int]$RegistrationTimeoutSeconds = 30,
    [ValidateRange(10, 600)][int]$PostgresTimeoutSeconds = 90,
    [ValidateRange(60, 600)][int]$ServiceStartupTimeoutSeconds = 240
)

$deploymentScriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { $deploymentScriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { throw 'Cannot determine this deployment script path. Run the .ps1 file directly.' }
$deploymentScriptDirectory = Split-Path -Parent $deploymentScriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $deploymentScriptDirectory }

$ErrorActionPreference = 'Stop'
$ServiceName = 'EnrichDSMDashboard'
$ServiceIdentity = "NT SERVICE\$ServiceName"
$AllowedCidrs = @('192.168.2.0/24', '192.168.41.0/24')
$PowerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator permissions are required. Open PowerShell with Run as administrator and run this installer again.'
}

function Write-InstallProgress([string]$Message) {
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function ConvertTo-WindowsArgument([AllowEmptyString()][string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append([char]34)
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) { $slashes++; continue }
        if ($character -eq [char]34) {
            for ($i = 0; $i -lt (2 * $slashes + 1); $i++) { [void]$builder.Append([char]92) }
            [void]$builder.Append([char]34)
            $slashes = 0
            continue
        }
        for ($i = 0; $i -lt $slashes; $i++) { [void]$builder.Append([char]92) }
        $slashes = 0
        [void]$builder.Append($character)
    }
    for ($i = 0; $i -lt (2 * $slashes); $i++) { [void]$builder.Append([char]92) }
    [void]$builder.Append([char]34)
    return $builder.ToString()
}

function Invoke-BoundedNativeCommand([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds, [string]$Label) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw "Could not start $Label." }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
            try { $process.Kill() } catch {}
            throw "$Label exceeded its $TimeoutSeconds second timeout and was stopped."
        }
        $process.WaitForExit()
        $output = @($stdoutTask.Result, $stderrTask.Result) -join [Environment]::NewLine
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = $output }
    } finally { $process.Dispose() }
}

function Invoke-Nssm([string[]]$NssmArguments) {
    $result = Invoke-BoundedNativeCommand $NssmPath $NssmArguments 30 ("NSSM {0}" -f $NssmArguments[0])
    if ($result.ExitCode -ne 0) { throw "NSSM command failed ($($NssmArguments[0])): $($result.Output.Trim())" }
}

function Get-ConfiguredServiceAccount([string]$Name) {
    $serviceConfig = Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f $Name.Replace("'", "''")) -ErrorAction Stop
    if (-not $serviceConfig) { return $null }
    return [string]$serviceConfig.StartName
}

function Set-AndVerifyVirtualServiceAccount([string]$Name) {
    if ($Name -notmatch '^[A-Za-z0-9_.-]+$') { throw "Invalid Windows service name '$Name'; refusing to derive a virtual service account." }
    $Account = "NT SERVICE\$Name"
    $currentAccount = Get-ConfiguredServiceAccount $Name
    if ($currentAccount -and $currentAccount.Trim() -ieq $account) {
        Write-InstallProgress "Verified '$Name' already uses $account; skipping redundant SCM account configuration."
        return
    }

    # NSSM 2.24 cannot configure virtual accounts reliably. Configure through
    # SCM, omitting password= so ChangeServiceConfig receives NULL as required
    # for a virtual service account (rather than a fabricated password).
    Write-InstallProgress "Configuring service identity for '$Name' as $account. Current SCM account: '$currentAccount'."
    $scPath = Join-Path $env:SystemRoot 'System32\sc.exe'
    $result = Invoke-BoundedNativeCommand $scPath @('config', $Name, 'obj=', $Account) 15 "SCM account configuration for $Name"

    # On some Windows/NSSM combinations sc.exe may return an error even though
    # the desired virtual account is already effective. Trust SCM readback only
    # after verifying the actual registered service account.
    $verifiedAccount = Get-ConfiguredServiceAccount $Name
    if ($verifiedAccount -and $verifiedAccount.Trim() -ieq $Account) {
        if ($result.ExitCode -ne 0) {
            Write-InstallProgress "sc.exe returned $($result.ExitCode), but SCM confirms '$Name' already runs as $Account; continuing."
        } else {
            Write-InstallProgress "SCM configured and verified '$Name' as $Account."
        }
        return
    }

    $actual = if ($verifiedAccount) { $verifiedAccount } else { '<service not registered>' }
    if ($result.ExitCode -ne 0) {
        throw "SCM account configuration failed for '$Name' (sc.exe exit $($result.ExitCode)). Expected '$Account'; SCM reports '$actual'. Output: $($result.Output.Trim())"
    }
    throw "Service '$Name' account verification failed. Expected '$Account'; SCM reports '$actual'."
}

function Wait-ForServiceStatus([string]$Name, [string]$Status, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $serviceStatus = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($serviceStatus -and $serviceStatus.Status.ToString() -eq $Status) { return $serviceStatus }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    $actual = if ($serviceStatus) { $serviceStatus.Status } else { 'not registered' }
    throw "Service '$Name' did not reach status '$Status' within $TimeoutSeconds seconds (current: $actual)."
}

function Test-TcpEndpoint([string]$HostName, [int]$TargetPort) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connectTask = $client.ConnectAsync($HostName, $TargetPort)
        return $connectTask.Wait(2000) -and $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

function Wait-ForPostgres([string]$HostName, [int]$TargetPort, [int]$TimeoutSeconds) {
    Write-InstallProgress "Waiting up to $TimeoutSeconds seconds for PostgreSQL at ${HostName}:${TargetPort}."
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-TcpEndpoint $HostName $TargetPort) {
            Write-InstallProgress 'PostgreSQL TCP readiness check passed.'
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "PostgreSQL at ${HostName}:${TargetPort} did not become reachable within $TimeoutSeconds seconds."
}

function Remove-ManagedNssmServiceIfPresent([string]$Name) {
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $existing) { return }
    $installedConfig = Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f $Name.Replace("'", "''")) -ErrorAction Stop
    if (-not $installedConfig -or $installedConfig.PathName -notmatch [regex]::Escape($NssmPath)) {
        throw "A service named '$Name' exists but is not registered to the configured NSSM executable. Refusing to stop or remove it."
    }
    Write-InstallProgress "Removing obsolete managed helper service '$Name'; startup preflight now runs inside '$ServiceName'."
    if ($existing.Status -ne 'Stopped') {
        Invoke-Nssm @('stop', $Name)
        Wait-ForServiceStatus $Name 'Stopped' 30 | Out-Null
    }
    Invoke-Nssm @('remove', $Name, 'confirm')
    $deadline = [DateTime]::UtcNow.AddSeconds($RegistrationTimeoutSeconds)
    while ((Get-Service -Name $Name -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Get-Service -Name $Name -ErrorAction SilentlyContinue) { throw "Managed helper service '$Name' remains registered after the bounded removal attempt." }
}

function Clear-OwnedDashboardListener([string]$Address, [int]$TargetPort) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalAddress $Address -LocalPort $TargetPort -ErrorAction SilentlyContinue)
    foreach ($listener in $listeners) {
        $processId = [int]$listener.OwningProcess
        $owner = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
        $expectedPython = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot '.venv\Scripts\python.exe'))
        $isOwnedDashboard = $owner -and $owner.ExecutablePath -and
            $owner.ExecutablePath.Equals($expectedPython, [StringComparison]::OrdinalIgnoreCase) -and
            $owner.CommandLine -match '(?i)(?:-m\s+uvicorn\s+backend\.main:app|backend\.main:app)'
        if (-not $isOwnedDashboard) {
            $processName = if ($owner) { [System.IO.Path]::GetFileName($owner.ExecutablePath) } else { '<process unavailable>' }
            throw "Port $TargetPort on $Address is occupied by PID $processId ($processName), which is not the project's known Uvicorn process. It was not stopped. Stop it manually or select an unused port."
        }

        Write-InstallProgress "Migrating the verified manual dashboard listener PID $processId off $Address`:$TargetPort."
        Stop-Process -Id $processId -Force -ErrorAction Stop
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        do {
            Start-Sleep -Milliseconds 500
            $remaining = Get-NetTCPConnection -State Listen -LocalAddress $Address -LocalPort $TargetPort -ErrorAction SilentlyContinue
        } while ($remaining -and [DateTime]::UtcNow -lt $deadline)
        if ($remaining) { throw "The verified dashboard PID $processId was stopped, but port $TargetPort did not become free within 20 seconds." }
        Write-InstallProgress "Verified the dashboard listener on $Address`:$TargetPort is stopped."
    }
}

function Set-AclGrant([string]$Path, [string]$Rights, [bool]$Recursive = $false, [string]$Account = $ServiceIdentity) {
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $args = @($Path, '/grant', "${Account}:(OI)(CI)($Rights)", '/C')
    if ($Recursive) { $args += '/T' }
    & icacls.exe @args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not grant $Rights access to required path: $Path" }
}

function Set-AclTraversalGrant([string]$Directory, [string]$Account = $ServiceIdentity) {
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { throw "Required path ancestor was not found: $Directory" }
    $directoryPath = (Resolve-Path -LiteralPath $Directory).Path
    $rights = [System.Security.AccessControl.FileSystemRights]::Traverse -bor
        [System.Security.AccessControl.FileSystemRights]::ReadAttributes -bor
        [System.Security.AccessControl.FileSystemRights]::Synchronize
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        [System.Security.Principal.NTAccount]::new($Account),
        $rights,
        [System.Security.AccessControl.InheritanceFlags]::None,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    $acl = Get-Acl -LiteralPath $directoryPath -ErrorAction Stop
    [void]$acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $directoryPath -AclObject $acl -ErrorAction Stop
}

function Grant-ServiceTraversalToAncestors([string]$Path, [string]$Account = $ServiceIdentity) {
    $resolvedPath = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $cursor = Split-Path -Parent $resolvedPath
    while ($cursor -and $cursor -ne [System.IO.Path]::GetPathRoot($cursor)) {
        Set-AclTraversalGrant $cursor $Account
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Read-ProjectEnvValue([string]$Key) {
    $line = Get-Content -LiteralPath (Join-Path $ProjectRoot '.env') | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

Write-InstallProgress 'Validating project, NSSM, existing Chromium cache, static IP and PostgreSQL service.'
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw "Project folder not found: $ProjectRoot" }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env') -PathType Leaf)) { throw 'Project .env is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -PathType Leaf)) { throw 'Project .venv is missing.' }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'frontend\dist\index.html') -PathType Leaf)) { throw 'Built frontend is missing; build frontend/dist first.' }
if (-not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) { throw "NSSM executable was not found at '$NssmPath'. Supply the verified NSSM path with -NssmPath." }
if (-not (Get-Service -Name $PostgresServiceName -ErrorAction SilentlyContinue)) { throw "PostgreSQL Windows service '$PostgresServiceName' was not found." }
if ($AllowedCidrs.Count -ne 2 -or $AllowedCidrs -notcontains '192.168.2.0/24' -or $AllowedCidrs -notcontains '192.168.41.0/24') { throw 'The approved LAN allowlist must remain 192.168.2.0/24 and 192.168.41.0/24.' }

$browserCache = $BrowserCachePath
if ([string]::IsNullOrWhiteSpace($browserCache)) { $browserCache = $env:PLAYWRIGHT_BROWSERS_PATH }
if ([string]::IsNullOrWhiteSpace($browserCache)) { $browserCache = Read-ProjectEnvValue 'PLAYWRIGHT_BROWSERS_PATH' }
if ([string]::IsNullOrWhiteSpace($browserCache)) { throw 'Set -BrowserCachePath or PLAYWRIGHT_BROWSERS_PATH to the existing Chromium cache folder.' }
if (-not [System.IO.Path]::IsPathRooted($browserCache)) { throw 'The Chromium cache path must be absolute.' }
if (-not (Test-Path -LiteralPath $browserCache -PathType Container)) { throw "Playwright Chromium cache is missing: $browserCache. Existing browsers will be reused; this installer does not download Chromium." }
$browserCache = (Resolve-Path -LiteralPath $browserCache).Path
$chromiumInstall = Get-ChildItem -LiteralPath $browserCache -Directory -Filter 'chromium-*' -ErrorAction SilentlyContinue | Select-Object -First 1
$headlessInstall = Get-ChildItem -LiteralPath $browserCache -Directory -Filter 'chromium_headless_shell-*' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $chromiumInstall -or -not $headlessInstall) { throw "The configured browser cache must contain chromium-* and chromium_headless_shell-* folders: $browserCache" }
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand -or -not (Test-Path -LiteralPath $nodeCommand.Source -PathType Leaf)) {
    throw 'Node.js is required by Playwright. Install an IT-approved Node.js runtime available to this Administrator session, then rerun the installer.'
}
$nodePath = (Resolve-Path -LiteralPath $nodeCommand.Source).Path

$listenIp = $null
if (-not [System.Net.IPAddress]::TryParse($ListenAddress, [ref]$listenIp) -or $listenIp.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'ListenAddress must be a machine IPv4 address.' }
$ipPresent = Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction SilentlyContinue
if (-not $ipPresent) { throw "IP $ListenAddress is not assigned to this machine. Confirm the static IP before installation." }

$pgService = Get-Service -Name $PostgresServiceName
if ($pgService.StartType -ne 'Automatic') { throw "Set PostgreSQL service '$PostgresServiceName' to Automatic before installing the dashboard service." }
if ($pgService.Status -ne 'Running') {
    Write-InstallProgress "Starting PostgreSQL Windows service '$PostgresServiceName'."
    Start-Service -Name $PostgresServiceName
    Wait-ForServiceStatus $PostgresServiceName 'Running' 60 | Out-Null
}
$pgHost = Read-ProjectEnvValue 'REPRO_PG_HOST'
if (-not $pgHost) { $pgHost = 'localhost' }
$pgPort = 5432
$pgPortText = Read-ProjectEnvValue 'REPRO_PG_PORT'
if ($pgPortText -and -not [int]::TryParse($pgPortText, [ref]$pgPort)) { throw 'REPRO_PG_PORT in .env must be an integer.' }
Wait-ForPostgres $pgHost $pgPort $PostgresTimeoutSeconds

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    $installedConfig = Get-CimInstance -ClassName Win32_Service -Filter ("Name='{0}'" -f $ServiceName) -ErrorAction Stop
    if (-not $installedConfig -or $installedConfig.PathName -notmatch [regex]::Escape($NssmPath)) {
        throw "A service named '$ServiceName' exists but is not registered to this NSSM executable. Refusing to change or stop it."
    }
}
if ($service -and $service.Status -ne 'Stopped') {
    Write-InstallProgress "Stopping the existing '$ServiceName' instance before reconfiguration."
    Invoke-Nssm @('stop', $ServiceName)
    Wait-ForServiceStatus $ServiceName 'Stopped' 30 | Out-Null
}
Clear-OwnedDashboardListener $ListenAddress $Port

$logs = Join-Path $ProjectRoot 'logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$serviceScript = Join-Path $deploymentScriptDirectory 'Start-LanDashboardService.ps1'
$serviceParameters = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -ProjectRoot "{1}" -ListenAddress {2} -Port {3} -AllowedCidrs "192.168.2.0/24,192.168.41.0/24"' -f $serviceScript, $ProjectRoot, $ListenAddress, $Port

Write-InstallProgress 'Registering or repairing the dashboard NSSM service.'
if (-not $service) { Invoke-Nssm @('install', $ServiceName, $PowerShellPath) }
Wait-ForServiceStatus $ServiceName 'Stopped' $RegistrationTimeoutSeconds | Out-Null
Invoke-Nssm @('set', $ServiceName, 'Application', $PowerShellPath)
Invoke-Nssm @('set', $ServiceName, 'AppDirectory', $ProjectRoot)
Invoke-Nssm @('set', $ServiceName, 'AppParameters', $serviceParameters)
Write-InstallProgress 'Configuring dashboard process supervision, delayed startup, PostgreSQL dependency and rotated logs.'
Invoke-Nssm @('set', $ServiceName, 'AppStdout', (Join-Path $logs 'lan-service.log'))
Invoke-Nssm @('set', $ServiceName, 'AppStderr', (Join-Path $logs 'lan-service-error.log'))
Invoke-Nssm @('set', $ServiceName, 'AppRotateFiles', '1')
Invoke-Nssm @('set', $ServiceName, 'AppRotateOnline', '1')
Invoke-Nssm @('set', $ServiceName, 'AppRotateBytes', '10485760')
Invoke-Nssm @('set', $ServiceName, 'AppExit', 'Default', 'Restart')
Invoke-Nssm @('set', $ServiceName, 'AppRestartDelay', '5000')
Invoke-Nssm @('set', $ServiceName, 'AppThrottle', '15000')
Invoke-Nssm @('set', $ServiceName, 'AppKillProcessTree', '1')
Invoke-Nssm @('set', $ServiceName, 'AppStopMethodConsole', '1500')
Invoke-Nssm @('set', $ServiceName, 'AppStopMethodWindow', '1500')
Invoke-Nssm @('set', $ServiceName, 'AppStopMethodThreads', '1500')
Invoke-Nssm @('set', $ServiceName, 'DependOnService', $PostgresServiceName)
Write-InstallProgress 'Persisting the existing Chromium cache path in the NSSM service environment.'
Invoke-Nssm @('set', $ServiceName, 'AppEnvironmentExtra', "PLAYWRIGHT_BROWSERS_PATH=$browserCache", "PLAYWRIGHT_NODEJS_PATH=$nodePath")
Invoke-Nssm @('set', $ServiceName, 'Start', 'SERVICE_DELAYED_AUTO_START')
& sc.exe sidtype $ServiceName unrestricted | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not enable the per-service Windows service SID.' }
Set-AndVerifyVirtualServiceAccount $ServiceName
& sc.exe description $ServiceName 'RE.Pro DSM dashboard LAN host; serves the React build and FastAPI from start_lan_dashboard.ps1.' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not set the dashboard service description.' }
& sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/30000 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not configure Windows service recovery actions.' }
& sc.exe failureflag $ServiceName 1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not enable recovery for non-zero service exits.' }
$failureReadback = Invoke-BoundedNativeCommand (Join-Path $env:SystemRoot 'System32\sc.exe') @('qfailure', $ServiceName) 15 'SCM recovery-action verification'
if ($failureReadback.ExitCode -ne 0 -or $failureReadback.Output -notmatch '(?i)RESTART') { throw "SCM restart recovery actions could not be verified. Output: $($failureReadback.Output.Trim())" }
Write-InstallProgress 'Verified Windows SCM restart-on-failure recovery actions.'

Write-InstallProgress 'Applying service-account file permissions.'
# The service uses a per-service virtual account, not LocalSystem or a user password.
# It gets read/execute on the app and write access only to runtime output folders.
Set-AclGrant $ProjectRoot 'RX' $true
foreach ($folder in @('downloads','uploads','artifacts','job_exports','logs')) { Set-AclGrant (Join-Path $ProjectRoot $folder) 'M' $true }
$downloadPath = Read-ProjectEnvValue 'REPRO_DOWNLOAD_DIR'
if (-not $downloadPath) { $downloadPath = 'downloads' }
if (-not [System.IO.Path]::IsPathRooted($downloadPath)) { $downloadPath = Join-Path $ProjectRoot $downloadPath }
Set-AclGrant ([System.IO.Path]::GetFullPath($downloadPath)) 'M' $true
Grant-ServiceTraversalToAncestors ([System.IO.Path]::GetFullPath($downloadPath))
if ($MasterExcelFolder) {
    $masterFolder = (Resolve-Path -LiteralPath $MasterExcelFolder).Path
    Set-AclGrant $masterFolder 'RX' $true
    Grant-ServiceTraversalToAncestors $masterFolder
}
$errorLog = Read-ProjectEnvValue 'REPRO_ERROR_LOG'
if (-not $errorLog) { $errorLog = 'download_errors.log' }
if (-not [System.IO.Path]::IsPathRooted($errorLog)) { $errorLog = Join-Path $ProjectRoot $errorLog }
$errorLogParent = Split-Path -Parent $errorLog
if ($errorLogParent -and -not (Test-Path -LiteralPath $errorLogParent)) { New-Item -ItemType Directory -Path $errorLogParent -Force | Out-Null }
if (-not (Test-Path -LiteralPath $errorLog)) { New-Item -ItemType File -Path $errorLog -Force | Out-Null }
& icacls.exe $errorLog /grant "${ServiceIdentity}:(M)" /C | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not grant the service account access to the report automation error log.' }
if ($errorLogParent) { Grant-ServiceTraversalToAncestors ([System.IO.Path]::GetFullPath($errorLogParent)) }
Set-AclGrant $browserCache 'RX' $true
Set-AclGrant (Split-Path -Parent $NssmPath) 'RX' $true
Set-AclGrant (Split-Path -Parent $nodePath) 'RX' $true
# The service can read the project/browser directories but also needs to walk
# their parent paths. Grant directory-only traverse/read-attributes access on
# ancestors; do not inherit permissions to sibling files or folders.
Write-InstallProgress 'Granting the virtual service account minimal traversal rights on runtime path ancestors.'
foreach ($runtimePath in @($ProjectRoot, $browserCache, (Split-Path -Parent $NssmPath), (Split-Path -Parent $nodePath))) {
    Grant-ServiceTraversalToAncestors $runtimePath
}

# Remove only the obsolete helper service installed by earlier revisions. The
# account-bound checks now run during the main service startup, under the real
# production service identity and inherited NSSM environment.
Remove-ManagedNssmServiceIfPresent 'EnrichDSMDashboardPreflight'
Write-InstallProgress "Python/BAT/Chromium preflight will run within '$ServiceName' after PostgreSQL and LAN-IP readiness."

Write-InstallProgress 'Starting the delayed-automatic dashboard service.'
try {
    Start-Service -Name $ServiceName -ErrorAction Stop
    Wait-ForServiceStatus $ServiceName 'Running' 45 | Out-Null
} catch {
    Start-Sleep -Seconds 2
    $wrapperError = Join-Path $logs 'lan-service-error.log'
    $wrapperTail = if (Test-Path -LiteralPath $wrapperError) { (Get-Content -LiteralPath $wrapperError -Tail 25) -join [Environment]::NewLine } else { '<wrapper error log not created>' }
    $serviceState = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    $stateText = if ($serviceState) { $serviceState.Status } else { 'not registered' }
    throw "Dashboard service failed during startup (state: $stateText). Latest wrapper diagnostics from ${wrapperError}:`n$wrapperTail"
}

$base = "http://${ListenAddress}:$Port"
$deadline = [DateTime]::UtcNow.AddSeconds($ServiceStartupTimeoutSeconds)
$health = $null
while ([DateTime]::UtcNow -lt $deadline) {
    try { $health = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 3; break } catch { Start-Sleep -Seconds 2 }
}
if (-not $health -or $health.status -ne 'ok') { throw 'Service started but its PostgreSQL health check did not succeed. Review logs/lan-service-error.log.' }
$page = Invoke-WebRequest -Uri $base -TimeoutSec 10 -UseBasicParsing
if ($page.StatusCode -ne 200 -or $page.Content -notmatch '<div id="root"></div>') { throw 'Service health is good but the React frontend did not return the expected page.' }

Write-Output "Service: $ServiceName"
Write-Output "Dashboard account: $ServiceIdentity"
Write-Output 'Startup: Automatic (Delayed Start)'
Write-Output "Status: $((Get-Service -Name $ServiceName).Status)"
Write-Output 'Recovery: Windows SCM restart actions verified; NSSM also restarts the supervised app on exit.'
Write-Output "Dashboard: $base"
Write-Output "Allowed CIDRs: $($AllowedCidrs -join ', ')"
Write-Output "Playwright browser cache: $browserCache"
Write-Output "PostgreSQL health: $($health.status)"
Write-Output 'Automation service-account preflight: runs at each service startup before Uvicorn; see logs/lan-service.log.'
Write-Output "Logs: $logs"
Write-Output 'Manual fallback remains: .\start_lan_dashboard.ps1 -ListenAddress 192.168.41.199 -Port 8080 -AdditionalAllowedCidrs @(''192.168.2.0/24'',''192.168.41.0/24'')'
