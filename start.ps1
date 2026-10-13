# Start Job Watch (Windows PowerShell)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$py = 'python'
if (Test-Path '.\.venv\Scripts\python.exe') { $py = '.\.venv\Scripts\python.exe' }

& $py -m app.cli init
& $py -m app.cli search-now
$listenHost = $env:JOBWATCH_HOST
if ([string]::IsNullOrWhiteSpace($listenHost)) { $listenHost = '0.0.0.0' }
$listenPort = $env:JOBWATCH_PORT
if ([string]::IsNullOrWhiteSpace($listenPort)) { $listenPort = '8000' }
Write-Host "Serveur sur http://$listenHost`:$listenPort (LAN: http://192.168.1.83:$listenPort)"
& $py -m uvicorn app.main:app --host $listenHost --port $listenPort