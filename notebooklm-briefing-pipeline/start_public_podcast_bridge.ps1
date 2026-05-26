$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$configPath = Join-Path $root 'config.json'
$config = Get-Content $configPath -Raw | ConvertFrom-Json
$publicUrl = 'https://podcast.example.com'
$config.podcast_bridge.base_url = $publicUrl
$config.podcast_bridge.image_url = "$publicUrl/static/cover.png"
$config | ConvertTo-Json -Depth 20 | Set-Content -Path $configPath -Encoding UTF8

$bridge = Start-Process python -ArgumentList 'podcast_bridge.py' -WorkingDirectory $root -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2

try {
    $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8788/healthz' -TimeoutSec 10
    if ($health.StatusCode -ne 200) {
        throw "bridge health check failed: $($health.StatusCode)"
    }
} catch {
    Stop-Process -Id $bridge.Id -Force -ErrorAction SilentlyContinue
    throw
}

$feedUrl = "$publicUrl/feed.xml"
$feedPath = Join-Path $root 'podcast_assets\public_feed_url.txt'
Set-Content -Path $feedPath -Value $feedUrl -Encoding UTF8

Write-Output "bridge_pid=$($bridge.Id)"
Write-Output "feed_url=$feedUrl"

