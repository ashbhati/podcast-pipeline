$ErrorActionPreference = 'Stop'

$mutex = New-Object System.Threading.Mutex($false, 'OpenClawPodcastBridgeSupervisor')
if (-not $mutex.WaitOne(0, $false)) {
    Write-Output 'supervisor already running'
    exit 0
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $root 'start_public_podcast_bridge.ps1'
$logDir = Join-Path $root 'logs'
$logPath = Join-Path $logDir 'podcast_bridge_supervisor.log'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Write-Log([string]$message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logPath -Value "[$stamp] $message"
}

function Test-BridgeHealthy {
    try {
        $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8788/healthz' -TimeoutSec 5
        if ($health.StatusCode -ne 200) { return $false }

        $feed = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8788/feed.xml' -TimeoutSec 10
        return $feed.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Stop-PodcastProcesses {
    $targets = Get-CimInstance Win32_Process | Where-Object {
        ($_.Name -ieq 'python.exe' -and $_.CommandLine -match 'podcast_bridge\.py')
    }

    foreach ($proc in $targets) {
        try {
            Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Log "stopped $($proc.Name) pid=$($proc.ProcessId)"
        } catch {}
    }
}

try {
    Write-Log 'supervisor started'
    while ($true) {
        if (Test-BridgeHealthy) {
            Start-Sleep -Seconds 30
            continue
        }

        Write-Log 'health check failed; restarting bridge stack'
        Stop-PodcastProcesses

        try {
            & $startScript | ForEach-Object { Write-Log $_ }
            Write-Log 'restart attempt completed'
        } catch {
            Write-Log "restart failed: $($_.Exception.Message)"
        }

        Start-Sleep -Seconds 20
    }
} finally {
    $mutex.ReleaseMutex() | Out-Null
    $mutex.Dispose()
}

