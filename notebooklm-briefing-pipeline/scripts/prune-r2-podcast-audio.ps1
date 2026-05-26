param(
  [string]$Bucket = 'your-podcast-audio-bucket',
  [double]$MaxGb = 8.0,
  [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$cache = Join-Path $PSScriptRoot '..\podcast_cache'
$limitBytes = [Int64]($MaxGb * 1GB)
$files = Get-ChildItem $cache -Filter '*.mp3' | ForEach-Object {
  if ($_.Name -match '^(\d{4}-\d{2}-\d{2})_(AM|PM|RESEARCH)_(.+)\.mp3$') {
    [PSCustomObject]@{
      File = $_
      Date = [DateTime]::ParseExact($Matches[1], 'yyyy-MM-dd', $null)
      NotebookId = $Matches[3]
      Key = "audio/$($Matches[3]).mp3"
      Size = $_.Length
    }
  }
} | Sort-Object -Property @{ Expression = { $_.Date }; Descending = $true }, @{ Expression = { $_.File.Name }; Descending = $true }

$kept = @()
$dropped = @()
$total = [Int64]0
foreach ($item in $files) {
  if (($total + $item.Size) -le $limitBytes) {
    $kept += $item
    $total += $item.Size
  } else {
    $dropped += $item
  }
}

Write-Host "R2 podcast FIFO plan for bucket '$Bucket'"
Write-Host "Limit: $MaxGb GB ($limitBytes bytes)"
Write-Host "Keep: $($kept.Count) files, $([Math]::Round($total / 1GB, 2)) GB"
Write-Host "Drop: $($dropped.Count) files"

foreach ($item in ($dropped | Sort-Object -Property @{ Expression = { $_.Date }; Descending = $false }, @{ Expression = { $_.File.Name }; Descending = $false })) {
  $cmd = "npx wrangler r2 object delete `"$Bucket/$($item.Key)`" --remote"
  if ($Apply) {
    Write-Host "DELETE $($item.Key)"
    npx wrangler r2 object delete "$Bucket/$($item.Key)" --remote
    if ($LASTEXITCODE -ne 0) { throw "Delete failed for $($item.Key)" }
  } else {
    Write-Host "DRY-RUN $cmd"
  }
}

if (-not $Apply) {
  Write-Host "Dry run only. Re-run with -Apply to delete older R2 objects."
}

