$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$serviceScript = Join-Path $root 'notebooklm_windows_service.py'
$python = 'C:\Python314\python.exe'
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}

$startupCmd = '--startup auto install'
& $python $serviceScript --startup auto install
& $python $serviceScript start

$startupDir = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$legacyLaunchers = @(
    (Join-Path $startupDir 'notebooklm-cloudflare-tunnel.cmd'),
    (Join-Path $startupDir 'notebooklm-podcast-bridge.cmd'),
    (Join-Path $startupDir 'openclaw-podcast-feed-bridge.cmd')
)

foreach ($path in $legacyLaunchers) {
    if (Test-Path $path) {
        Rename-Item -Path $path -NewName ($path + '.disabled-service') -Force
    }
}

Write-Output 'NotebookLM Windows service installed and started.'

