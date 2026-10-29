# Windows UI wrapper. Requires an existing Ubuntu/Debian WSL distribution.
# Installs/configures PostgreSQL/PostGIS/Mapnik ONLY INSIDE WSL, never on Windows.
param(
    [Parameter(Position=0)] [string]$PbfPath,
    [Parameter(Position=1)] [string]$ZoomOverride
)
$ErrorActionPreference = 'Stop'
$endToEndTimer = [Diagnostics.Stopwatch]::StartNew()
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

function Fail([string]$message, [int]$code = 1) {
    Write-Host "`n[ERROR] $message" -ForegroundColor Red
    exit $code
}
function Resolve-Pbf([string]$requested) {
    if ($requested) {
        if (-not (Test-Path -LiteralPath $requested -PathType Leaf)) {
            Fail "PBF file not found: $requested"
        }
        return (Resolve-Path -LiteralPath $requested).Path
    }
    $matches = @(Get-ChildItem -LiteralPath $project -File -Filter '*.osm.pbf' -ErrorAction SilentlyContinue)
    if ($matches.Count -eq 1) { return $matches[0].FullName }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.OpenFileDialog
        $dialog.Title = 'Choose an OSM PBF to convert to Raster MBTiles'
        $dialog.Filter = 'OpenStreetMap PBF (*.osm.pbf;*.pbf)|*.osm.pbf;*.pbf|All files (*.*)|*.*'
        $dialog.CheckFileExists = $true
        $dialog.Multiselect = $false
        if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
            return $dialog.FileName
        }
        Fail 'PBF selection was canceled.' 2
    } catch {
        if ($_.Exception.Message -match 'canceled') { throw }
        $raw = Read-Host 'PBF file path (paste or drag here, then Enter)'
        $raw = $raw.Trim('"').Trim()
        if (-not (Test-Path -LiteralPath $raw -PathType Leaf)) {
            Fail "PBF file not found: $raw"
        }
        return (Resolve-Path -LiteralPath $raw).Path
    }
}

# Global default: select an existing local OSM PBF (or auto-use the sole
# *.osm.pbf in the project directory). Do not implicitly download a country.
# Legacy South Korea download remains explicit: run.bat south-korea.
# run.bat vector / both can also choose a local PBF.
$format = if ($env:OSM2MBTILES_FORMAT) { $env:OSM2MBTILES_FORMAT.ToLowerInvariant() } else { 'raster' }
if ($PbfPath -in @('raster','vector','both')) {
    $format = $PbfPath
    $PbfPath = ''
}
if ($format -notin @('raster','vector','both')) { Fail 'Format must be raster, vector or both.' }
if ($PbfPath -match '^\d+(?:-\d+)?$' -and -not $ZoomOverride) {
    $ZoomOverride = $PbfPath
    $PbfPath = ''
}
if ($PbfPath -eq 'south-korea') {
    $pbf = '--geofabrik-south-korea'
    $inputLabel = 'https://download.geofabrik.de/asia/south-korea-latest.osm.pbf'
    $output = Join-Path $project 'south-korea-latest.mbtiles'
} else {
    # Generic default: choose a local OSM PBF instead of assuming a country.
    $pbf = Resolve-Pbf $PbfPath
    $inputLabel = $pbf
    $filename = [System.IO.Path]::GetFileName($pbf)
    if ($filename -match '(?i)\.osm\.pbf$') { $basename = $filename.Substring(0, $filename.Length - 8) }
    elseif ($filename -match '(?i)\.pbf$') { $basename = $filename.Substring(0, $filename.Length - 4) }
    else { Fail 'Input must be a .pbf or .osm.pbf file.' }
    $output = Join-Path ([System.IO.Path]::GetDirectoryName($pbf)) "$basename.mbtiles"
}
# Output names distinguish the new format without overwriting existing PNG maps.
if ($format -eq 'vector') { $output = [IO.Path]::ChangeExtension($output, $null).TrimEnd('.') + '.vector.mbtiles' }
$zoom = if ($ZoomOverride) { $ZoomOverride } elseif ($env:OSM2MBTILES_ZOOM) { $env:OSM2MBTILES_ZOOM } else { '0-15' }
$workers = if ($env:OSM2MBTILES_PROCESSES) { $env:OSM2MBTILES_PROCESSES } else { '4' }
$importProfile = if ($env:OSM2MBTILES_VECTOR_IMPORT_PROFILE) { $env:OSM2MBTILES_VECTOR_IMPORT_PROFILE.ToLowerInvariant() } else { 'safe' }
if ($importProfile -notin @('safe', 'fast')) { Fail 'OSM2MBTILES_VECTOR_IMPORT_PROFILE must be safe or fast.' }
$vectorTiler = if ($env:OSM2MBTILES_VECTOR_TILER) { $env:OSM2MBTILES_VECTOR_TILER.ToLowerInvariant() } else { 'postgis' }
if ($vectorTiler -notin @('postgis','batch')) { Fail 'OSM2MBTILES_VECTOR_TILER must be postgis or batch.' }
$batchSide = if ($env:OSM2MBTILES_VECTOR_BATCH_SIDE) { $env:OSM2MBTILES_VECTOR_BATCH_SIDE } else { '2' }
if ($batchSide -notmatch '^[1-8]$') { Fail 'OSM2MBTILES_VECTOR_BATCH_SIDE must be 1..8.' }
$rasterFormat = if ($env:OSM2MBTILES_RASTER_FORMAT) { $env:OSM2MBTILES_RASTER_FORMAT.ToLowerInvariant() } else { 'png' }
if ($rasterFormat -notin @('png','png256')) { Fail 'OSM2MBTILES_RASTER_FORMAT must be png or png256.' }
$bbox = if ($env:OSM2MBTILES_BBOX) { $env:OSM2MBTILES_BBOX.Trim() } else { '' }
$queryMode = if ($env:OSM2MBTILES_VECTOR_QUERY_MODE) { $env:OSM2MBTILES_VECTOR_QUERY_MODE.ToLowerInvariant() } else { 'projected' }
if ($queryMode -notin @('projected','baseline','cached')) { Fail 'OSM2MBTILES_VECTOR_QUERY_MODE must be projected, baseline, or cached.' }
$cachePolicy = if ($env:OSM2MBTILES_CLEANUP) { $env:OSM2MBTILES_CLEANUP.ToLowerInvariant() } else { 'compact' }
if ($cachePolicy -notin @('keep', 'compact', 'prune')) { Fail 'OSM2MBTILES_CLEANUP must be keep, compact, or prune.' }
if ($zoom -notmatch '^(?:\d+)(?:-\d+)?$') { Fail "Invalid zoom: $zoom" }
if ($workers -notmatch '^\d+$' -or [int]$workers -lt 1 -or [int]$workers -gt 64) { Fail "Invalid worker count: $workers" }

Write-Host "`n=== osm2mbtiles - Docker-free one-click launcher ===" -ForegroundColor Cyan
Write-Host "Input : $inputLabel"
Write-Host "Output: $output"
Write-Host "Zoom  : $zoom   Workers: $workers   Cache: $cachePolicy   Format: $format   Vector SQL: $queryMode   Import: $importProfile"
Write-Host "Vector tiler: $vectorTiler   batch side: $batchSide   Raster: $rasterFormat   BBOX: $(if ($bbox) {$bbox} else {'PBF header'})"
Write-Host 'Backend: Linux tools inside WSL (no Docker)' -ForegroundColor DarkCyan

$outputs = if ($format -eq 'both') {
    @(([IO.Path]::ChangeExtension($output, $null).TrimEnd('.') + '.raster.mbtiles'),
      ([IO.Path]::ChangeExtension($output, $null).TrimEnd('.') + '.vector.mbtiles'))
} else { @($output) }
if (@($outputs | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count -gt 0) {
    $answer = Read-Host 'Output already exists. Replace it? [y/N]'
    if ($answer -notin @('y','Y','yes','YES')) { Fail 'Canceled; existing output was preserved.' 2 }
}

if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Write-Host 'First-time prerequisite: install Ubuntu WSL, then restart Windows if prompted:'
    Write-Host '  wsl --install -d Ubuntu' -ForegroundColor Yellow
    Fail 'WSL is not installed. Native Windows Mapnik/PostGIS binaries are not included.' 3
}
# The installed default distro must be a real Ubuntu/Debian distro with apt-get.
$preflightOutput = & wsl.exe --exec bash -lc 'command -v apt-get >/dev/null && command -v wslpath >/dev/null && echo WSL_READY' 2>&1
if ($LASTEXITCODE -ne 0 -or ($preflightOutput -join "`n") -notmatch 'WSL_READY') {
    Write-Host ($preflightOutput -join "`n")
    Write-Host 'Install/set an Ubuntu or Debian default WSL distro:' -ForegroundColor Yellow
    Write-Host '  wsl --install -d Ubuntu'
    Write-Host '  wsl --set-default Ubuntu'
    Fail 'A working Ubuntu/Debian WSL distribution is required.' 3
}

$script = Join-Path $PSScriptRoot 'run_wsl.sh'
# Convert a Windows path to its WSL location; call wslpath for robust drive/UNC handling.
$scriptUnix = (& wsl.exe --exec wslpath -a $script 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $scriptUnix) { Fail 'Unable to translate launcher path into WSL.' }
Write-Host "`n[INFO] Starting native Mapnik/PostGIS pipeline..." -ForegroundColor Cyan
& wsl.exe --exec bash $scriptUnix $project $pbf $output $zoom $workers $cachePolicy $format $queryMode $importProfile $vectorTiler $batchSide $rasterFormat $bbox
$code = $LASTEXITCODE
if ($code -ne 0) { Fail "WSL rendering failed (exit $code). See output above." $code }
foreach ($produced in $outputs) {
    if (-not (Test-Path -LiteralPath $produced -PathType Leaf)) {
        Fail "Runner finished but output was not created: $produced"
    }
    Write-Host "`n[SUCCESS] $format MBTiles: $produced" -ForegroundColor Green
}
$endToEndTimer.Stop()
Write-Host ("[TIME] Windows end-to-end elapsed (WSL + verify + copy): {0:N3}s ({1})" -f $endToEndTimer.Elapsed.TotalSeconds,$endToEndTimer.Elapsed.ToString('hh\:mm\:ss')) -ForegroundColor Cyan
exit 0
