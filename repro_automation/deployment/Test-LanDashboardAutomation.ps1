[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$BrowserCachePath = '',
    [string]$ExpectedServiceAccount = ''
)

$deploymentScriptPath = $PSCommandPath
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { $deploymentScriptPath = $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($deploymentScriptPath)) { throw 'Cannot determine this deployment script path. Run the .ps1 file directly.' }
$deploymentScriptDirectory = Split-Path -Parent $deploymentScriptPath
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $ProjectRoot = Split-Path -Parent $deploymentScriptDirectory }

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$bat = Join-Path $ProjectRoot 'download_reports.bat'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project Python environment is missing.' }
if (-not (Test-Path -LiteralPath $bat -PathType Leaf)) { throw 'download_reports.bat is missing.' }

if ([string]::IsNullOrWhiteSpace($BrowserCachePath)) { $BrowserCachePath = $env:PLAYWRIGHT_BROWSERS_PATH }
if ([string]::IsNullOrWhiteSpace($BrowserCachePath)) { throw 'PLAYWRIGHT_BROWSERS_PATH is missing; set it in the NSSM service environment.' }
if (-not (Test-Path -LiteralPath $BrowserCachePath -PathType Container)) { throw "Playwright browser cache is inaccessible: $BrowserCachePath" }
$BrowserCachePath = (Resolve-Path -LiteralPath $BrowserCachePath).Path
$chromium = Get-ChildItem -LiteralPath $BrowserCachePath -Directory -Filter 'chromium-*' -ErrorAction SilentlyContinue | Select-Object -First 1
$headlessShell = Get-ChildItem -LiteralPath $BrowserCachePath -Directory -Filter 'chromium_headless_shell-*' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $chromium -or -not $headlessShell) { throw "Expected chromium-* and chromium_headless_shell-* installations are missing from $BrowserCachePath." }

if ([string]::IsNullOrWhiteSpace($env:PLAYWRIGHT_NODEJS_PATH)) { throw 'PLAYWRIGHT_NODEJS_PATH is missing from the NSSM service environment.' }
if (-not (Test-Path -LiteralPath $env:PLAYWRIGHT_NODEJS_PATH -PathType Leaf)) { throw "Configured Playwright Node executable is inaccessible: $env:PLAYWRIGHT_NODEJS_PATH" }
if ($ExpectedServiceAccount) {
    $actualIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ($actualIdentity -ine $ExpectedServiceAccount) { throw "Automation preflight identity mismatch. Expected '$ExpectedServiceAccount'; running as '$actualIdentity'." }
}

$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserCachePath
$env:PLAYWRIGHT_NODEJS_PATH = (Resolve-Path -LiteralPath $env:PLAYWRIGHT_NODEJS_PATH).Path

function Write-PreflightProgress([string]$Message) {
    Write-Output ('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message)
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

function Invoke-BoundedProcess([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSeconds, [string]$Label, [string]$RawArgumentLine = '') {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    if ($RawArgumentLine) { $startInfo.Arguments = $RawArgumentLine }
    else { $startInfo.Arguments = (($Arguments | ForEach-Object { ConvertTo-WindowsArgument ([string]$_) }) -join ' ') }
    $startInfo.WorkingDirectory = $ProjectRoot
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
        if ($process.ExitCode -ne 0) {
            $safeOutput = $output -replace '(?i)(password|passwd|secret|token)(\s*[:=]\s*)\S+', '$1$2[REDACTED]'
            if ($safeOutput.Length -gt 1200) { $safeOutput = $safeOutput.Substring(0, 1200) + ' [truncated]' }
            throw "$Label failed with exit code $($process.ExitCode). Diagnostic output: $($safeOutput.Trim())"
        }
    } finally { $process.Dispose() }
}

Write-PreflightProgress "Automation preflight identity: $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
Write-PreflightProgress 'Checking FastAPI/Uvicorn/PostgreSQL backend imports (30-second limit).'
Invoke-BoundedProcess $python @('-c', 'from backend.main import app; assert app is not None') 30 'Python backend dependency preflight'

Write-PreflightProgress 'Checking Python downloader CLI (30-second limit).'
Invoke-BoundedProcess $python @((Join-Path $ProjectRoot 'repro_download.py'), '--help') 30 'Python report automation CLI preflight'

Write-PreflightProgress 'Checking the configured Node.js runtime (10-second limit).'
Invoke-BoundedProcess $env:PLAYWRIGHT_NODEJS_PATH @('--version') 10 'Node.js runtime preflight'

Write-PreflightProgress 'Checking download_reports.bat argument forwarding without submitting a portal download (30-second limit).'
$batArgumentLine = '/d /s /c ""{0}" --help"' -f $bat
Invoke-BoundedProcess $env:ComSpec @() 30 'BAT-to-Python command preflight' $batArgumentLine

$browserProbe = @'
import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        await browser.close()
asyncio.run(asyncio.wait_for(main(), timeout=30))
'@
Write-PreflightProgress 'Launching and closing headless Chromium from the configured browser cache (45-second process limit).'
Invoke-BoundedProcess $python @('-c', $browserProbe) 45 'Headless Chromium launch preflight'

Write-PreflightProgress 'AUTOMATION PREFLIGHT OK: Python CLI, BAT forwarding, and headless Chromium launch all succeeded.'
