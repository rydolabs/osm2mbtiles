param([ValidateSet('status','prune')][string]$Action = 'status')
$ErrorActionPreference = 'Stop'
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Host '[ERROR] WSL is not installed.' -ForegroundColor Red
    exit 3
}
$path = Join-Path $PSScriptRoot 'cache_cleanup.sh'
$scriptUnix = (& wsl.exe --exec wslpath -a $path 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $scriptUnix) {
    Write-Host '[ERROR] Unable to locate the WSL cache management script.' -ForegroundColor Red
    exit 3
}
# Only remove the cached Korean MBTiles copy if the verified Windows output
# still exists. Never delete a potential sole copy of an arbitrary export.
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$completedOutput = Join-Path $project 'south-korea-latest.mbtiles'
$hasCopy = Test-Path -LiteralPath $completedOutput -PathType Leaf
$exportName = if ($Action -eq 'prune' -and $hasCopy) { 'south-korea-latest.mbtiles' } else { '' }
$verifiedWindowsPath = ''
if ($exportName) {
    $verifiedWindowsPath = (& wsl.exe --exec wslpath -a $completedOutput 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $verifiedWindowsPath) {
        Write-Host '[ERROR] Unable to verify Windows output file.' -ForegroundColor Red
        exit 3
    }
}
& wsl.exe --exec bash $scriptUnix $Action $exportName --manual $verifiedWindowsPath
exit $LASTEXITCODE