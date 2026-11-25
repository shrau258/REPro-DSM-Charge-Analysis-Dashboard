Set-Location 'C:\Users\scadacc1\Downloads\REPro_Python_Backend\repro_automation'
Set-ExecutionPolicy -Scope Process Bypass -Force

$project = (Get-Location).Path
$nssm = 'C:\nssm\nssm.exe\win64\nssm.exe'

if (-not (Test-Path -LiteralPath $nssm -PathType Leaf)) {
    throw "NSSM was not found at $nssm. Set `$nssm to its verified executable path and rerun."
}

.\deployment\Install-LanDashboardService.ps1 `
  -ProjectRoot $project `
  -NssmPath $nssm `
  -ListenAddress '192.168.41.199' `
  -Port 8080 `
  -PostgresServiceName 'postgresql-x64-18' `
  -BrowserCachePath 'C:\Users\scadacc1\AppData\Local\ms-playwright'
