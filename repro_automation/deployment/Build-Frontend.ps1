[CmdletBinding()]
param([string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot))
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
