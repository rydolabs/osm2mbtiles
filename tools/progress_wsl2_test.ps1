# Actual Windows + Ubuntu WSL2 progress-display verification.
# Double-click TEST-Progress-WSL2.bat. PowerShell 5.1 compatible.
# Uses actual project Windows launcher, no mocked WSL or map features.
[CmdletBinding()]
param(
    [string]$ReportRoot = '',
    [switch]$ArchiveSelfTest,
    [switch]$ArchiveSelfTestFail
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $ReportRoot) { $ReportRoot = Join-Path $root 'progress-test-results' }
New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null
$runId = 'qa_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + (Get-Random -Minimum 1000 -Maximum 9999)
$work = Join-Path $ReportRoot $runId
$data = Join-Path $work 'local-data'
$raw = Join-Path $work 'raw-logs'
$evidence = Join-Path $work 'zip-evidence'
$logs = Join-Path $evidence 'logs'
foreach ($path in @($data, $raw, $evidence, $logs)) {
    New-Item -ItemType Directory -Path $path -Force | Out-Null
}
$archive = Join-Path $ReportRoot ($runId + '.zip')
$state = [ordered]@{
    schema = 'osm2mbtiles-progress-wsl2-evidence-v1'
    run_id = $runId
    mode = 'real_windows_wsl2'
    verdict = 'FAIL'
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    completed_utc = $null
    windows_version = [Environment]::OSVersion.Version.ToString()
    powershell_version = $PSVersionTable.PSVersion.ToString()
    pbf_source_url = 'https://download.geofabrik.de/europe/monaco-latest.osm.pbf'
    pbf_sha256 = $null
    pbf_bytes = $null
    zoom = '12-16'
    bbox = '7.41,43.72,7.44,43.75'
    wsl_probe = $null
    steps = [ordered]@{
        checkout = 'NOT_RUN'
        wsl2 = 'NOT_RUN'
        pbf = 'NOT_RUN'
        both_raster_vector = 'NOT_RUN'
        measured_progress = 'NOT_RUN'
        mbtiles_integrity = 'NOT_RUN'
    }
    failure = $null
    note = 'NO automatic upload. ZIP excludes PBF, MBTiles, DB and raw logs. Review ZIP before sharing.'
}
$exitCode = 1

function Write-Utf8([string]$File, [string]$Value) {
    [IO.File]::WriteAllText($File, $Value, [Text.UTF8Encoding]::new($false))
}

function Redact([string]$Value) {
    if (-not $Value) { return '' }
    $text = $Value
    if ($work) { $text = $text.Replace($work, '<LOCAL_TEST_DIR>') }
    if ($root) { $text = $text.Replace($root, '<SOURCE_CHECKOUT>') }
    if ($env:USERPROFILE) { $text = $text.Replace($env:USERPROFILE, '<USERPROFILE>') }
    if ($env:USERNAME) { $text = $text.Replace($env:USERNAME, '<USER>') }
    $text = [regex]::Replace($text, '(?i)[A-Z]:\\Users\\[^\\\s"''<>]+', '<USERPROFILE>')
    $text = [regex]::Replace($text, '(?i)/mnt/[a-z]/Users/[^/\s"''<>]+', '<USERPROFILE_WSL>')
    $text = [regex]::Replace($text, '/home/[^/\s"''<>]+', '/home/<USER>')
    $text = [regex]::Replace($text, '(?i)(authorization|token|password|api[_-]?key|secret)\s*[:=]\s*(?:"[^"]+"|\S+)', '$1=<REDACTED>')
    $text = [regex]::Replace($text, '(https?://[^\s?]+)\?[^\s"''<>]+', '$1?<QUERY_REDACTED>')
    return $text
}

function Stage([string]$Name, [string]$Result) {
    $state['steps'][$Name] = $Result
    Write-Host ('[CHECK] ' + $Name + ': ' + $Result)
}

function Invoke-Logged([string]$Command, [string[]]$Arguments, [string]$LogPath) {
    # Children print live in the same terminal; Tee-Object also saves output.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command @Arguments 2>&1 | Tee-Object -FilePath $LogPath
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($null -eq $result) { return 1 }
    return [int]$result
}

function Wsl-Path([string]$File) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & wsl.exe --exec wslpath -a $File 2>&1
        $result = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($result -ne 0) { throw 'Windows path could not be translated into WSL.' }
    $path = ($out | Out-String).Trim()
    if (-not $path.StartsWith('/')) { throw 'WSL did not return an absolute path.' }
    return $path
}

function Safe-Logs {
    foreach ($log in @(Get-ChildItem -LiteralPath $raw -File -ErrorAction SilentlyContinue)) {
        $text = Get-Content -LiteralPath $log.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $text) { $text = '(empty log)' }
        $text = Redact ([string]$text)
        if ($text.Length -gt 800000) {
            $text = '[LOG TRUNCATED: final 800000 characters]' + [Environment]::NewLine +
                    $text.Substring($text.Length - 800000)
        }
        Write-Utf8 (Join-Path $logs $log.Name) $text
    }
}

function Save-ProofSafely {
    $source = Join-Path $raw 'progress-proof-original.json'
    if (Test-Path -LiteralPath $source -PathType Leaf) {
        $proof = Get-Content -LiteralPath $source -Raw
        Write-Utf8 (Join-Path $evidence 'progress-proof.json') (Redact $proof)
    }
}

try {
    Write-Host ''
    Write-Host '==== osm2mbtiles real Windows + WSL2 progress test ====' -ForegroundColor Cyan
    Write-Host 'Real input: Geofabrik Monaco PBF; mode: BOTH (Mapnik Raster + PostGIS Vector)'
    Write-Host 'Progress: 6 setup stages + measured tile %, tiles/sec, ETA'
    Write-Host ('Local results: ' + $work)
    Write-Host 'Input data, full MBTiles and raw logs remain on this computer. Nothing is uploaded.'
    if ($ArchiveSelfTest -or $ArchiveSelfTestFail) {
        $state['mode'] = 'archive_self_test_only'
        $state['verdict'] = if ($ArchiveSelfTestFail) { 'SIMULATED_FAIL' } else { 'SELF_TEST_ONLY' }
        Stage 'checkout' 'SELF_TEST_ONLY'
        Write-Utf8 (Join-Path $raw 'sensitive-fixture.log') 'C:\Users\Alice\secret.osm.pbf token=abc123 password=example'
        if ($ArchiveSelfTestFail) {
            throw 'Intentional ZIP failure-path self-test; no WSL2 execution attempted.'
        }
        $exitCode = 0
    } else {
        foreach ($name in @(
            'pyproject.toml', 'tools/run_windows.ps1', 'tools/run_wsl.sh',
            'tools/cache_layout.sh', 'tools/progress_wsl2_verify.py'
        )) {
            if (-not (Test-Path -LiteralPath (Join-Path $root $name) -PathType Leaf)) {
                throw ('Incomplete source ZIP: ' + $name)
            }
        }
        Stage 'checkout' 'PASS'

        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
            throw 'WSL not installed. Install an Ubuntu WSL2 distribution first.'
        }
        $env:OSM2MBTILES_TEST_RUN_ID = $runId
        $wslEntries = @()
        if ($env:WSLENV) { $wslEntries = @($env:WSLENV -split ':') }
        if ($wslEntries -notcontains 'OSM2MBTILES_TEST_RUN_ID') {
            $wslEntries += 'OSM2MBTILES_TEST_RUN_ID'
        }
        $env:WSLENV = $wslEntries -join ':'
        $probe = 'set -e; . /etc/os-release; echo DISTRO=$ID; uname -r; command -v apt-get; echo QA_ID=$OSM2MBTILES_TEST_RUN_ID'
        $probeExit = Invoke-Logged 'wsl.exe' @('--exec', 'bash', '-lc', $probe) (Join-Path $raw 'wsl2-preflight.log')
        $probeText = Get-Content -LiteralPath (Join-Path $raw 'wsl2-preflight.log') -Raw
        if ($probeExit -ne 0 -or $probeText -notmatch 'microsoft.*WSL2' -or
            $probeText -notmatch 'DISTRO=(ubuntu|debian)' -or
            $probeText -notmatch [regex]::Escape('QA_ID=' + $runId)) {
            throw 'A working default Ubuntu/Debian WSL2 distribution with WSLENV forwarding is required. Check: wsl --list --verbose'
        }
        $state['wsl_probe'] = Redact $probeText.Trim()
        Stage 'wsl2' 'PASS'

        # Some native packages require sudo in Ubuntu. Ask through the console
        # before starting the fully live piped launcher.
        $uid = Invoke-Logged 'wsl.exe' @('--exec', 'id', '-u') (Join-Path $raw 'wsl-uid.log')
        if ($uid -ne 0) { throw 'WSL user ID check failed.' }
        $uidText = (Get-Content -LiteralPath (Join-Path $raw 'wsl-uid.log') -Raw).Trim()
        if ($uidText -ne '0') {
            Write-Host '[INFO] Ubuntu sudo may prompt for the Ubuntu password (not the Windows password).'
            $sudoExit = Invoke-Logged 'wsl.exe' @('--exec', 'sudo', '-v') (Join-Path $raw 'wsl-sudo.log')
            if ($sudoExit -ne 0) { throw 'Ubuntu sudo permission check failed.' }
        }

        $pbf = Join-Path $data 'monaco-progress.osm.pbf'
        $partial = $pbf + '.part'
        for ($attempt=1; $attempt -le 3; $attempt++) {
            try {
                Write-Host ('[DOWNLOAD] Geofabrik Monaco, attempt ' + $attempt)
                Invoke-WebRequest -Uri $state['pbf_source_url'] -OutFile $partial -UseBasicParsing -TimeoutSec 240
                if ((Get-Item -LiteralPath $partial).Length -lt 25000) {
                    throw 'PBF too small to be an OSM extract.'
                }
                Move-Item -LiteralPath $partial -Destination $pbf -Force
                break
            } catch {
                Write-Utf8 (Join-Path $raw ('pbf-download-' + $attempt + '.log')) (Redact $_.Exception.Message)
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds (3*$attempt)
            }
        }
        $state['pbf_bytes'] = (Get-Item -LiteralPath $pbf).Length
        $state['pbf_sha256'] = (Get-FileHash -LiteralPath $pbf -Algorithm SHA256).Hash.ToLowerInvariant()
        Stage 'pbf' 'PASS'

        # Use the project's actual Windows->WSL launcher, not a separate fake
        # renderer. Private WSL cache is selected by OSM2MBTILES_TEST_RUN_ID.
        $env:OSM2MBTILES_FORMAT = 'both'
        $env:OSM2MBTILES_ZOOM = '12-16'
        $env:OSM2MBTILES_BBOX = $state['bbox']
        $env:OSM2MBTILES_PROCESSES = '2'
        $env:OSM2MBTILES_CLEANUP = 'prune'
        $env:OSM2MBTILES_VECTOR_TILER = 'postgis'
        $env:OSM2MBTILES_VECTOR_IMPORT_PROFILE = 'safe'
        $env:OSM2MBTILES_VECTOR_QUERY_MODE = 'projected'
        $env:OSM2MBTILES_RASTER_FORMAT = 'png'
        $raster = Join-Path $data 'monaco-progress.raster.mbtiles'
        $vector = Join-Path $data 'monaco-progress.vector.mbtiles'
        $launcher = Join-Path $root 'tools/run_windows.ps1'
        $conversionLog = Join-Path $raw 'live-conversion.log'
        Write-Host '[RUN] Actual Mapnik Raster and PostGIS Vector rendering, with LIVE PROGRESS:'
        $convertExit = Invoke-Logged 'powershell.exe' @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $launcher, $pbf, '12-16'
        ) $conversionLog
        if ($convertExit -ne 0) { throw ('Real Windows+WSL2 BOTH conversion failed (exit ' + $convertExit + ').') }
        Stage 'both_raster_vector' 'PASS'

        $script = Wsl-Path (Join-Path $root 'tools/progress_wsl2_verify.py')
        $logUnix = Wsl-Path $conversionLog
        $rasterUnix = Wsl-Path $raster
        $vectorUnix = Wsl-Path $vector
        $proofPath = Join-Path $raw 'progress-proof-original.json'
        $proofUnix = Wsl-Path $proofPath
        $previewUnix = Wsl-Path (Join-Path $evidence 'preview-mapnik.png')
        $checkExit = Invoke-Logged 'wsl.exe' @(
            '--exec', '/usr/bin/python3', $script,
            '--log', $logUnix, '--raster', $rasterUnix,
            '--vector', $vectorUnix, '--report', $proofUnix,
            '--preview', $previewUnix
        ) (Join-Path $raw 'progress-verify.log')
        if ($checkExit -ne 0) { throw ('Measured progress/MBTiles verification failed (exit ' + $checkExit + ').') }
        $proof = Get-Content -LiteralPath $proofPath -Raw | ConvertFrom-Json
        if (-not $proof.passed) { throw 'Real progress verifier did not pass.' }
        Stage 'measured_progress' 'PASS'
        Stage 'mbtiles_integrity' 'PASS'
        $state['verdict'] = 'PASS'
        $exitCode = 0
    }
} catch {
    $state['failure'] = Redact $_.Exception.Message
    if ($state['verdict'] -ne 'SIMULATED_FAIL') { $state['verdict'] = 'FAIL' }
    Write-Host ('[FAIL] ' + $state['failure']) -ForegroundColor Red
    $exitCode = 1
} finally {
    $state['completed_utc'] = (Get-Date).ToUniversalTime().ToString('o')
    try {
        Safe-Logs
        Save-ProofSafely
        Write-Utf8 (Join-Path $evidence 'summary.json') (ConvertTo-Json -InputObject $state -Depth 12)
        $readme = @(
            '# osm2mbtiles real Windows+WSL2 progress display test'
            ''
            ('Verdict: ' + $state['verdict'])
            ('Mode: ' + $state['mode'])
            ('Run ID: ' + $runId)
            ('Source SHA256: ' + $state['pbf_sha256'])
            ''
            'Stages:'
        )
        foreach ($key in $state['steps'].Keys) {
            $readme += ('- ' + $key + ': ' + $state['steps'][$key])
        }
        if ($state['failure']) { $readme += ('Failure: ' + $state['failure']) }
        $readme += @(
            ''
            'Checks: actual WSL2, PBF import, Mapnik PNG rendering, PostGIS gzip MVT,'
            'measured intermediate percent, positive throughput, ETA, six stages and byte-verified Windows copy.'
            ''
            'A ZIP existing DOES NOT mean the real test passed. Require verdict=PASS and mode=real_windows_wsl2.'
            ''
            'Review the redacted ZIP before sharing. Full MBTiles, PBF, and raw logs are excluded.'
            'Map data: © OpenStreetMap contributors, ODbL, https://www.openstreetmap.org/copyright'
        )
        Write-Utf8 (Join-Path $evidence 'README-RESULTS.txt') ($readme -join [Environment]::NewLine)
        $manifest = @()
        foreach ($file in @(Get-ChildItem -LiteralPath $evidence -File -Recurse | Sort-Object FullName)) {
            $rel = $file.FullName.Substring($evidence.Length + 1).Replace('\', '/')
            $sha = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $manifest += ($sha + '  ' + $rel)
        }
        Write-Utf8 (Join-Path $evidence 'evidence-sha256.txt') ($manifest -join [Environment]::NewLine)
        Compress-Archive -Path (Join-Path $evidence '*') -DestinationPath $archive -CompressionLevel Optimal -Force
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zipRead = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            $entries = @($zipRead.Entries | ForEach-Object { $_.FullName.Replace('\','/') })
            if ($entries -notcontains 'summary.json' -or
                $entries -notcontains 'README-RESULTS.txt' -or
                $entries -notcontains 'evidence-sha256.txt') {
                throw 'ZIP is missing required evidence files.'
            }
        } finally {
            $zipRead.Dispose()
        }
        Write-Host ''
        Write-Host ('[ZIP] ' + $archive) -ForegroundColor Cyan
        Write-Host ('[VERDICT] ' + $state['verdict'])
        Write-Host '[NOTE] The ZIP is not uploaded; local data and raw logs stay on your computer.'
    } catch {
        Write-Host ('[ZIP ERROR] ' + $_.Exception.Message) -ForegroundColor Red
        $exitCode = 2
    }
}
exit $exitCode
