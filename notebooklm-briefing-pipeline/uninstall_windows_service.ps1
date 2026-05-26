$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$serviceScript = Join-Path $root 'notebooklm_windows_service.py'
$python = 'C:\Python314\python.exe'
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

try { & $python $serviceScript stop } catch {}
try { & $python $serviceScript remove } catch {}

$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
Get-ChildItem $startupDir -Filter '*.disabled-service' -ErrorAction SilentlyContinue | ForEach-Object {
    $restoredName = $_.Name -replace '\.disabled-service$',''
    Rename-Item -Path $_.FullName -NewName $restoredName -Force
}

Write-Output 'NotebookLM Windows service removed. Legacy startup launchers restored if present.'

