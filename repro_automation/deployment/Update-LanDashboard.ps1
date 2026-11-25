[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{40}$')][string]$ApprovedCommit,
    [string]$NssmPath = 'C:\nssm\nssm.exe\win64\ssm.exe',
    [string]$PostgresBinPath = '',
    [string]$ListenAddress = '192.168.41.199',
    [ValidateRange(1024, 65535)][int]$Port = 8080
)

$scriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($scriptPath)) { $scriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($scriptPath)) { throw 'Cannot determine this script path. Run the .ps1 file directly.' }
$scriptDirectory = Split-Path -Parent $scriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $scriptDirectory }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path
$ErrorActionPreference = 'Stop'
$serviceName = 'EnrichDSMDashboard'
$serviceStopped = $false
$updated = $false

function Write-UpdateProgress([string]$Message) {
    Write-Host ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
}

function Invoke-Git([string[]]$Arguments) {
    & git -C $script:ProjectRoot @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git command failed ($($Arguments -join ' ')); exit code $LASTEXITCODE." }
}

function Wait-DashboardHealth([int]$Seconds = 90) {
    $url = "http://${ListenAddress}:$Port/api/health"
    $deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri $url -TimeoutSec 5
            if ($health.status -eq 'ok') { return }
        } catch {}
        Start-Sleep -Seconds 2
    }
    throw "Dashboard/PostgreSQL health did not recover within $Seconds seconds at $url."
}

$repoRoot = (& git -C $ProjectRoot rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) { throw 'ProjectRoot is not inside a Git working tree.' }
$repoRoot = $repoRoot.Trim()
$dirty = & git -C $repoRoot status --porcelain
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect Git working-tree status.' }
if ($dirty) { throw 'Refusing production update because the Git working tree has uncommitted or untracked files. Commit or safely relocate them first.' }
if (-not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) { throw "NSSM not found: $NssmPath" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run approved production updates from Administrator PowerShell because stopping/restarting the Windows service requires elevation.' }
$service = Get-Service -Name $serviceName -ErrorAction Stop
$serviceConfig = Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" -ErrorAction Stop
if (-not $serviceConfig -or $serviceConfig.PathName -notmatch [regex]::Escape($NssmPath)) { throw "'$serviceName' is not registered to the configured NSSM executable." }
$oldCommit = (& git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Could not determine current production commit.' }

Write-UpdateProgress "Fetching only the explicitly approved commit $ApprovedCommit from origin."
& git -C $repoRoot fetch origin $ApprovedCommit
if ($LASTEXITCODE -ne 0) { throw 'Could not fetch the approved commit. No service or project files were changed.' }
& git -C $repoRoot cat-file -e ("{0}^{{commit}}" -f $ApprovedCommit)
if ($LASTEXITCODE -ne 0) { throw 'ApprovedCommit is not a valid commit object.' }
& git -C $repoRoot merge-base --is-ancestor $oldCommit $ApprovedCommit
if ($LASTEXITCODE -ne 0) { throw 'ApprovedCommit is not a fast-forward descendant of the current version; refusing a history rewrite.' }
if ($ApprovedCommit -ieq $oldCommit) { Write-Output 'The requested approved commit is already deployed.'; exit 0 }

$backupScript = Join-Path $scriptDirectory 'Backup-Postgres.ps1'
Write-UpdateProgress 'Taking and verifying a PostgreSQL backup before updating application files.'
$backupResult = @(& $backupScript -ProjectRoot $ProjectRoot -PostgresBinPath $PostgresBinPath)
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed; the service was not stopped and the application was not updated.' }
$backupFile = $backupResult | Select-Object -Last 1
if (-not $backupFile -or -not (Test-Path -LiteralPath ([string]$backupFile) -PathType Leaf)) { throw 'Database backup did not return a verified backup file path.' }

try {
    Write-UpdateProgress 'Stopping the NSSM service for the fast-forward update.'
    & $NssmPath stop $serviceName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'NSSM could not stop the dashboard service.' }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do { Start-Sleep -Milliseconds 500; $service = Get-Service -Name $serviceName } while ($service.Status -ne 'Stopped' -and [DateTime]::UtcNow -lt $deadline)
    if ($service.Status -ne 'Stopped') { throw 'Dashboard service did not stop within 45 seconds.' }
    $serviceStopped = $true

    Write-UpdateProgress "Fast-forwarding the clean working tree from $oldCommit to $ApprovedCommit."
    Invoke-Git @('merge','--ff-only',$ApprovedCommit)
    $updated = $true

    Write-UpdateProgress 'Installing the approved Python dependency set.'
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    & $python -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

    Write-UpdateProgress 'Installing locked frontend dependencies and building production assets.'
    Push-Location (Join-Path $ProjectRoot 'frontend')
    try {
        npm.cmd ci
        if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'npm run build failed.' }
    } finally { Pop-Location }

    Write-UpdateProgress 'Starting the updated NSSM service and checking FastAPI/PostgreSQL health.'
    Start-Service -Name $serviceName
    $serviceStopped = $false
    Wait-DashboardHealth 120
    Write-Output "Update succeeded. Commit: $ApprovedCommit; verified DB backup: $backupFile"
} catch {
    $updateError = $_
    if ($updated) {
        Write-Warning "Update failed. Attempting application rollback to $oldCommit; the PostgreSQL backup is preserved at $backupFile."
        try {
            if (-not $serviceStopped) {
                & $NssmPath stop $serviceName | Out-Null
                $rollbackDeadline = [DateTime]::UtcNow.AddSeconds(45)
                do { Start-Sleep -Milliseconds 500; $currentService = Get-Service -Name $serviceName } while ($currentService.Status -ne 'Stopped' -and [DateTime]::UtcNow -lt $rollbackDeadline)
                if ($currentService.Status -ne 'Stopped') { throw 'Could not stop updated service for rollback.' }
                $serviceStopped = $true
            }
            $rollbackDirty = & git -C $repoRoot status --porcelain
            if ($LASTEXITCODE -ne 0) { throw 'Could not inspect worktree before rollback.' }
            if ($rollbackDirty) { throw 'Worktree changed during deployment; refusing git reset to avoid deleting concurrent changes. Restore the previous release manually after review.' }
            & git -C $repoRoot reset --hard $oldCommit
            if ($LASTEXITCODE -ne 0) { throw 'Git could not restore the previous application commit.' }
            & (Join-Path $ProjectRoot '.venv\Scripts\python.exe') -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')
            if ($LASTEXITCODE -ne 0) { throw 'Could not reinstall previous Python dependencies.' }
            Push-Location (Join-Path $ProjectRoot 'frontend')
            try {
                npm.cmd ci
                if ($LASTEXITCODE -ne 0) { throw 'Could not reinstall previous frontend dependencies.' }
                npm.cmd run build
                if ($LASTEXITCODE -ne 0) { throw 'Could not rebuild the previous frontend.' }
            } finally { Pop-Location }
            Start-Service -Name $serviceName
            $serviceStopped = $false
            Wait-DashboardHealth 120
            Write-Warning "Application files were rolled back to $oldCommit and health checks passed. Database restore was not performed automatically; the verified backup is retained at $backupFile."
        } catch {
            Write-Error "Automatic application rollback was not fully validated: $($_.Exception.Message). Database backup: $backupFile"
        }
    }
    throw $updateError
}
