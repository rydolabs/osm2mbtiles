# Run Seoul OSM PBF -> actual PNG/PBF MBTiles using existing WSL launcher.
# Download is cached and atomic. The total timer includes download, setup,
# PostGIS import, rendering, verification and Windows copy.
param(
    [Parameter(Position=0)] [string]$Zoom = '12-14',
    [Parameter(Position=1)]
    [ValidateSet('raster', 'vector', 'both')]
    [string]$TileType = 'both'
)
$ErrorActionPreference = 'Stop'
$timer = [Diagnostics.Stopwatch]::StartNew()
$code = 0
try {
    if ($Zoom -notmatch '^([0-9]+)(?:-([0-9]+))?$') {
        throw 'Zoom must be e.g. 12-14 or 15.'
    }
    $lo = [int]$Matches[1]
    $hi = if ($Matches[2]) { [int]$Matches[2] } else { $lo }
    if ($lo -gt $hi -or $hi -gt 22) { throw 'Zoom must be between 0 and 22, low <= high.' }
    $project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
    $outputDir = Join-Path $project 'test-output'
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
    $pbf = Join-Path $outputDir 'seoul-non-military.osm.pbf'
    $source = 'https://tiles.osm.kr/download/seoul-non-military.osm.pbf'
    if (-not (Test-Path -LiteralPath $pbf -PathType Leaf) -or (Get-Item -LiteralPath $pbf).Length -lt 1048576) {
        $part = "$pbf.download"
        Write-Host "[SEOUL] Downloading $source" -ForegroundColor Cyan
        $succeeded = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Invoke-WebRequest -Uri $source -OutFile $part -UseBasicParsing -TimeoutSec 300
                if ((Get-Item -LiteralPath $part).Length -lt 1048576) {
                    throw 'Downloaded PBF is unexpectedly small.'
                }
                Move-Item -LiteralPath $part -Destination $pbf -Force
                $succeeded = $true
                break
            } catch {
                Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds (2 * $attempt)
            }
        }
        if (-not $succeeded) { throw 'Could not download Seoul PBF.' }
    } else {
        Write-Host "[SEOUL] Reusing cached Seoul PBF: $pbf"
    }
    # Pass the requested format and the proven region batch engine to the
    # existing Windows + WSL pipeline; do not shortcut PNG Carto generation.
    $env:OSM2MBTILES_FORMAT = $TileType
    if (-not $env:OSM2MBTILES_VECTOR_TILER) {
        $env:OSM2MBTILES_VECTOR_TILER = 'batch'
    }
    if (-not $env:OSM2MBTILES_VECTOR_BATCH_SIDE) {
        $env:OSM2MBTILES_VECTOR_BATCH_SIDE = '2'
    }
    if (-not $env:OSM2MBTILES_RASTER_FORMAT) {
        $env:OSM2MBTILES_RASTER_FORMAT = 'png'
    }
    # Seoul extract may not carry a usable OSM PBF HeaderBBox. Always supply a
    # deterministic region to avoid a preflight exit(2); caller can override it.
    if (-not $env:OSM2MBTILES_BBOX) {
        $env:OSM2MBTILES_BBOX = '126.75,37.40,127.20,37.72'
    }
    Write-Host "[SEOUL] Generating tile-type=$TileType zoom=$Zoom vector-engine=$env:OSM2MBTILES_VECTOR_TILER bbox=$env:OSM2MBTILES_BBOX" -ForegroundColor Cyan
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $project 'tools/run_windows.ps1') $pbf $Zoom
    if ($LASTEXITCODE -ne 0) { throw "WSL converter exited with code $LASTEXITCODE" }
    $stem = Join-Path $outputDir 'seoul-non-military'
    $expected = @()
    if ($TileType -eq 'both') {
        $expected = @("$stem.raster.mbtiles", "$stem.vector.mbtiles")
    } elseif ($TileType -eq 'vector') {
        $expected = @("$stem.vector.mbtiles")
    } else {
        $expected = @("$stem.mbtiles")
    }
    foreach ($path in $expected) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-Item -LiteralPath $path).Length -eq 0) {
            throw "Missing or empty result: $path"
        }
        Write-Host "[SEOUL] VERIFIED: $path" -ForegroundColor Green
    }
} catch {
    $code = 1
    Write-Host "[SEOUL] ERROR: $($_.Exception.Message)" -ForegroundColor Red
} finally {
    $timer.Stop()
    Write-Host ("[TIME] Seoul total elapsed (download + import + PNG/PBF + verify + copy): {0:N3}s ({1})" -f $timer.Elapsed.TotalSeconds, $timer.Elapsed.ToString('hh\:mm\:ss')) -ForegroundColor Cyan
}
exit $code
