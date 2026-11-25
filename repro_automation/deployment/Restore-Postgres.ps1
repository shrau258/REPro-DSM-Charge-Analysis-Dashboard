[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [Parameter(Mandatory = $true)][string]$BackupFile,
    [Parameter(Mandatory = $true)][string]$TargetDatabase,
    [Parameter(Mandatory = $true)][string]$ConfirmTargetDatabase,
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

if ($TargetDatabase -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$') { throw 'TargetDatabase must be a simple PostgreSQL database identifier (letters, digits, underscores; max 63 characters).' }
if ($ConfirmTargetDatabase -cne $TargetDatabase) { throw 'Confirmation failed: ConfirmTargetDatabase must exactly match TargetDatabase.' }
if (-not (Test-Path -LiteralPath $BackupFile -PathType Leaf)) { throw "Backup file not found: $BackupFile" }
$BackupFile = (Resolve-Path -LiteralPath $BackupFile).Path
$hostName = Read-EnvValue 'REPRO_PG_HOST'; if (-not $hostName) { $hostName = 'localhost' }
$port = Read-EnvValue 'REPRO_PG_PORT'; if (-not $port) { $port = '5432' }
$username = Read-EnvValue 'REPRO_PG_USER'; if (-not $username) { $username = 'postgres' }
$password = Read-EnvValue 'REPRO_PG_PASSWORD'
if (-not $password) { throw 'REPRO_PG_PASSWORD is empty in .env; refusing to run a restore.' }
if ($PostgresBinPath) {
    $pgRestore = Join-Path $PostgresBinPath 'pg_restore.exe'
    $createdb = Join-Path $PostgresBinPath 'createdb.exe'
    $dropdb = Join-Path $PostgresBinPath 'dropdb.exe'
} else {
    $pgRestore = (Get-Command pg_restore.exe -ErrorAction SilentlyContinue).Source
    $createdb = (Get-Command createdb.exe -ErrorAction SilentlyContinue).Source
    $dropdb = (Get-Command dropdb.exe -ErrorAction SilentlyContinue).Source
}
foreach ($tool in @($pgRestore,$createdb,$dropdb)) { if (-not $tool -or -not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw 'pg_restore.exe, createdb.exe, or dropdb.exe not found. Add PostgreSQL bin to PATH or pass -PostgresBinPath.' } }

$oldPassword = $env:PGPASSWORD
$createdTarget = $false
try {
    $env:PGPASSWORD = $password
    & $pgRestore --list $BackupFile *> $null
    if ($LASTEXITCODE -ne 0) { throw 'The selected file is not a readable pg_dump custom-format archive.' }
    & $createdb --host $hostName --port $port --username $username --maintenance-db postgres --template template0 --encoding UTF8 --no-password --quiet $TargetDatabase
    if ($LASTEXITCODE -ne 0) { throw "Could not create target database '$TargetDatabase'. Restore only targets a new database; existing databases are never replaced." }
    $createdTarget = $true

    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Restoring archive into database '$TargetDatabase'."
    & $pgRestore --host $hostName --port $port --username $username --dbname $TargetDatabase --no-owner --no-privileges --exit-on-error $BackupFile
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE." }
    Write-Host "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Restore completed. Verify dashboard schema/data before changing application .env to this database."
} catch {
    if ($createdTarget) {
        Write-Warning "Restore did not complete. Removing the newly created target database '$TargetDatabase' to avoid leaving a partial restore."
        & $dropdb --host $hostName --port $port --username $username --maintenance-db postgres --no-password --if-exists $TargetDatabase *> $null
    }
    throw
} finally {
    $env:PGPASSWORD = $oldPassword
    $password = $null
}
