[CmdletBinding()]
param([string]$ProjectRoot = '')

$deploymentScriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { $deploymentScriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { throw 'Cannot determine this deployment script path. Run the .ps1 file directly.' }
$deploymentScriptDirectory = Split-Path -Parent $deploymentScriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $deploymentScriptDirectory }

$ErrorActionPreference = 'Stop'
$frontend = Join-Path (Resolve-Path -LiteralPath $ProjectRoot).Path 'frontend'
Push-Location $frontend
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'npm ci failed.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'npm run build failed.' }
} finally { Pop-Location }
Write-Host 'Production frontend built in frontend/dist.'
