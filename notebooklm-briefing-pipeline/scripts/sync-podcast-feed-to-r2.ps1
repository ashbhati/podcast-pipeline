$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir 'podcast_feed_r2_sync.log'
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $logPath -Value "[$stamp] sync start"

$syncScript = Join-Path $root 'scripts\sync_podcast_feed_to_r2.py'
$healthScript = Join-Path $root 'scripts\podcast_feed_health_check.py'

try {
    & 'C:\Python314\python.exe' $syncScript --recent 20 *>&1 | Tee-Object -FilePath $logPath -Append
    $syncExitCode = $LASTEXITCODE
} catch {
    $syncExitCode = 1
    Add-Content -Path $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] sync exception=$($_.Exception.Message)"
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $logPath -Value "[$stamp] sync exit=$syncExitCode"

# Always run the guard, even if sync failed. It can identify the exact missing
# feed/audio invariant and perform one recovery sync when audio is now ready.
try {
    & 'C:\Python314\python.exe' $healthScript --recent 20 --check 6 --recover *>&1 | Tee-Object -FilePath $logPath -Append
    $healthExitCode = $LASTEXITCODE
} catch {
    $healthExitCode = 1
    Add-Content -Path $logPath -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] health exception=$($_.Exception.Message)"
}

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $logPath -Value "[$stamp] health exit=$healthExitCode"

if ($syncExitCode -ne 0) { exit $syncExitCode }
if ($healthExitCode -ne 0) { exit $healthExitCode }

