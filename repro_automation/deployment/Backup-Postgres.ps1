[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$BackupDirectory = '',
    [string]$PostgresBinPath = ''
)

$scriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($scriptPath)) { $scriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($scriptPath)) { throw 'Cannot determine this script path. Run the .ps1 file directly.' }
$scriptDirectory = Split-Path -Parent $scriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $scriptDirectory }
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot -ErrorAction Stop).Path

function Read-EnvValue([string]$Key) {
    $envPath = Join-Path $ProjectRoot '.env'
    $line = Get-Content -LiteralPath $envPath | Where-Object { $_ -match "^\s*$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
    if (-not $line) { return '' }
    return ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
}

if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot '.env') -PathType Leaf)) { throw 'Project .env is missing.' }
$hostName = Read-EnvValue 'REPRO_PG_HOST'; if (-not $hostName) { $hostName = 'localhost' }
$port = Read-EnvValue 'REPRO_PG_PORT'; if (-not $port) { $port = '5432' }
$database = Read-EnvValue 'REPRO_PG_DATABASE'; if (-not $database) { $database = 'repro_reports' }
$username = Read-EnvValue 'REPRO_PG_USER'; if (-not $username) { $username = 'postgres' }
$password = Read-EnvValue 'REPRO_PG_PASSWORD'
if (-not $password) { throw 'REPRO_PG_PASSWORD is empty in .env; refusing to run an unauthenticated backup.' }
if (-not $BackupDirectory) { $BackupDirectory = Join-Path $ProjectRoot 'backups' }
New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
$BackupDirectory = (Resolve-Path -LiteralPath $BackupDirectory).Path
if ($PostgresBinPath) {
    $pgDump = Join-Path $PostgresBinPath 'pg_dump.exe'
    $pgRestore = Join-Path $PostgresBinPath 'pg_restore.exe'
} else {
    $pgDump = (Get-Command pg_dump.exe -ErrorAction SilentlyContinue).Source
    $pgRestore = (Get-Command pg_restore.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pgDump -or -not (Test-Path -LiteralPath $pgDump -PathType Leaf)) { throw 'pg_dump.exe not found. Add PostgreSQL bin to PATH or pass -PostgresBinPath.' }
if (-not $pgRestore -or -not (Test-Path -LiteralPath $pgRestore -PathType Leaf)) { throw 'pg_restore.exe not found. Add PostgreSQL bin to PATH or pass -PostgresBinPath.' }

$oldPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = $password
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupFile = Join-Path $BackupDirectory ("{0}-{1}.dump" -f $database, $stamp)
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Creating PostgreSQL custom-format backup: $backupFile"
    & $pgDump --host $hostName --port $port --username $username --dbname $database --format=custom --no-owner --no-privileges --file $backupFile
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE." }
    if (-not (Test-Path -LiteralPath $backupFile) -or (Get-Item -LiteralPath $backupFile).Length -eq 0) { throw 'pg_dump did not create a non-empty backup file.' }
    & $pgRestore --list $backupFile *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Backup verification with pg_restore --list failed.' }
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Backup and archive verification succeeded. Store a copy on an IT-approved protected backup target."
    Write-Output $backupFile
} finally {
    $env:PGPASSWORD = $oldPassword
    $password = $null
}
