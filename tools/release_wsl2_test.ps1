# One-click local Windows+Ubuntu WSL2 E2E smoke. PowerShell 5.1-compatible.
# Designed to be started by TEST-Windows-WSL2.bat in the same source checkout.
# Never uploads data; result ZIP includes only redacted diagnostics and preview.
[CmdletBinding()]
param(
    [string]$ReportRoot = '',
    [switch]$ArchiveSelfTest,
    [switch]$ArchiveSelfTestFailure
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $ReportRoot) { $ReportRoot = Join-Path $project 'release-test-results' }
New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null

$id = 'qa_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + (Get-Random -Minimum 1000 -Maximum 9999)
$run = Join-Path $ReportRoot $id
$raw = Join-Path $run 'raw-logs'
$evidence = Join-Path $run 'zip-evidence'
$inputs = Join-Path $run 'inputs-and-outputs'
$logs = Join-Path $evidence 'logs'
foreach ($dir in @($raw, $evidence, $inputs, $logs)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
$archive = Join-Path $ReportRoot ($id + '.zip')
$report = [ordered]@{
    schema = 'osm2mbtiles-windows-wsl2-release-evidence-v1'
    run_id = $id
    mode = 'real_windows_wsl2'
    result = 'FAIL'
    started_utc = (Get-Date).ToUniversalTime().ToString('o')
    completed_utc = $null
    source = 'https://download.geofabrik.de/europe/monaco-latest.osm.pbf'
    source_sha256 = $null
    source_size_bytes = $null
    windows_version = [Environment]::OSVersion.Version.ToString()
    powershell_version = $PSVersionTable.PSVersion.ToString()
    wsl_probe = $null
    steps = [ordered]@{
        source_checkout = 'NOT_RUN'
        wsl2_preflight = 'NOT_RUN'
        monaco_download = 'NOT_RUN'
        launcher_both = 'NOT_RUN'
        launcher_vector = 'NOT_RUN'
        output_validation = 'NOT_RUN'
    }
    output_files = @()
    failure = $null
    privacy = 'Zip excludes raw input PBF, complete MBTiles, PostgreSQL DB, raw logs, usernames and absolute file paths.'
}
$zipExit = 1

function Write-PlainText([string]$path, [string]$value) {
    [IO.File]::WriteAllText($path, $value, ([Text.UTF8Encoding]::new($false)))
}

function Hide-LocalData([string]$value) {
    if (-not $value) { return '' }
    $text = $value
    if ($run) { $text = $text.Replace($run, '<LOCAL_TEST_DIR>') }
    if ($project) { $text = $text.Replace($project, '<SOURCE_CHECKOUT>') }
    if ($env:USERPROFILE) { $text = $text.Replace($env:USERPROFILE, '<USERPROFILE>') }
    if ($env:USERNAME) { $text = $text.Replace($env:USERNAME, '<USER>') }
    $text = [regex]::Replace($text, '(?i)[A-Z]:\\Users\\[^\\\s"]+', '<USERPROFILE>')
    $text = [regex]::Replace($text, '(?i)/mnt/[a-z]/Users/[^/\s"]+', '<USERPROFILE_WSL>')
    $text = [regex]::Replace($text, '/home/[^/\s"]+', '/home/<USER>')
    $text = [regex]::Replace($text, '(?i)(authorization|api[_-]?key|password|secret|token)\s*[:=]\s*("[^"]+"|[^\s]+)', '$1=<REDACTED>')
    $text = [regex]::Replace($text, '(https?://[^\s?]+)\?[^\s"]+', '$1?<QUERY_REDACTED>')
    return $text
}

function Copy-SafeLogs {
    foreach ($file in @(Get-ChildItem -LiteralPath $raw -File -ErrorAction SilentlyContinue)) {
        $value = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
        if (-not $value) { $value = '(no captured output)' }
        $value = Hide-LocalData ([string]$value)
        if ($value.Length -gt 350000) {
            $value = '[Truncated to last 350000 characters]' + [Environment]::NewLine + $value.Substring($value.Length - 350000)
        }
        Write-PlainText (Join-Path $logs $file.Name) $value
    }
}

function Write-Check([string]$name, [string]$state) {
    $report['steps'][$name] = $state
    Write-Host ('[CHECK] ' + $name + ': ' + $state)
}

function Run-Logged([string]$file, [string[]]$argv, [string]$logfile) {
    # Print output live while retaining diagnostics. Keeping stdin attached to
    # the console allows Ubuntu sudo to prompt for its password if needed.
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $file @argv 2>&1 | Tee-Object -FilePath $logfile
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($null -eq $code) { return 1 }
    return [int]$code
}

function Wsl-Path([string]$windowsPath) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & wsl.exe --exec wslpath -a $windowsPath 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
    if ($code -ne 0) { throw 'wslpath could not translate a local test file.' }
    $converted = ($lines | Out-String).Trim()
    if (-not $converted.StartsWith('/')) { throw 'WSL returned an invalid translated path.' }
    return $converted
}

try {
    Write-Host ''
    Write-Host '== osm2mbtiles: actual Windows + WSL2 release test ==' -ForegroundColor Cyan
    Write-Host 'Input: small Geofabrik Monaco OSM extract (downloaded automatically)'
    Write-Host 'Runs: existing Windows launcher BOTH; existing launcher VECTOR; validate generated archives'
    Write-Host 'Output: a small redacted ZIP plus local originals (no upload)'
    Write-Host ('Evidence folder: ' + $run)

    if ($ArchiveSelfTest -or $ArchiveSelfTestFailure) {
        $report['mode'] = 'archive_self_test_only'
        $report['result'] = if ($ArchiveSelfTestFailure) { 'SIMULATED_FAIL' } else { 'NOT_A_REAL_WSL_TEST' }
        $report['steps']['source_checkout'] = 'SELF_TEST_ONLY'
        Write-PlainText (Join-Path $raw 'sensitive-fixture.log') 'C:\Users\Alice\secret\input.osm.pbf token=abc123 password=example'
        if ($ArchiveSelfTestFailure) {
            throw 'Intentional archive-test failure. No WSL conversion was attempted.'
        }
        $zipExit = 0
    } else {
        foreach ($required in @(
            'pyproject.toml', 'run.bat', 'tools/run_windows.ps1',
            'tools/run_wsl.sh', 'tools/cache_layout.sh',
            'tools/release_wsl2_verify.py'
        )) {
            if (-not (Test-Path -LiteralPath (Join-Path $project $required) -PathType Leaf)) {
                throw ('Incomplete checkout, required file missing: ' + $required)
            }
        }
        Write-Check 'source_checkout' 'PASS'

        if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
            throw 'WSL is missing. Install Ubuntu WSL2, reboot if requested, and rerun.'
        }
        # A valid WSL2 kernel and Ubuntu/Debian apt are both required. Pass an
        # explicit run ID into WSLENV to ensure caches do not touch user jobs.
        $env:OSM2MBTILES_TEST_RUN_ID = $id
        $items = @()
        if ($env:WSLENV) { $items = @($env:WSLENV -split ':') }
        if ($items -notcontains 'OSM2MBTILES_TEST_RUN_ID') {
            $items += 'OSM2MBTILES_TEST_RUN_ID'
        }
        $env:WSLENV = ($items -join ':')
        $probe = 'set -e; . /etc/os-release; echo DISTRO=$ID; uname -r; command -v apt-get; echo QA_ID=$OSM2MBTILES_TEST_RUN_ID'
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $wslLines = & wsl.exe --exec bash -lc $probe 2>&1
            $wslExit = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $old
        }
        $wslText = ($wslLines | Out-String)
        Write-PlainText (Join-Path $raw 'wsl-preflight.log') $wslText
        if ($wslExit -ne 0 -or $wslText -notmatch 'microsoft.*WSL2' -or
            $wslText -notmatch 'DISTRO=(ubuntu|debian)' -or
            $wslText -notmatch [regex]::Escape('QA_ID=' + $id)) {
            throw 'Default WSL distribution must be working Ubuntu/Debian on WSL2 with WSLENV support. Run: wsl --list --verbose'
        }
        $report['wsl_probe'] = Hide-LocalData $wslText.Trim()
        Write-Check 'wsl2_preflight' 'PASS'

        # Pre-authenticate Ubuntu sudo while stdin is an attached console.
        # This does not elevate Windows and does not change its execution policy.
        $old = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $uid = (& wsl.exe --exec id -u 2>&1 | Out-String).Trim()
            if ($uid -ne '0') {
                Write-Host '[INFO] Ubuntu may ask for your sudo password to install native packages.'
                & wsl.exe --exec sudo -v
                $sudoCode = $LASTEXITCODE
                if ($sudoCode -ne 0) { throw 'Ubuntu sudo check failed.' }
            }
        } finally {
            $ErrorActionPreference = $old
        }

        $sourceUrl = $report['source']
        $bothPbf = Join-Path $inputs 'monaco-release-both.osm.pbf'
        $vectorPbf = Join-Path $inputs 'monaco-release-vector.osm.pbf'
        $downloadPart = $bothPbf + '.part'
        $downloaded = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                Write-Host ('[DOWNLOAD] Monaco PBF attempt ' + $attempt)
                Invoke-WebRequest -Uri $sourceUrl -OutFile $downloadPart -UseBasicParsing -TimeoutSec 240
                if ((Get-Item -LiteralPath $downloadPart).Length -lt 25000) {
                    throw 'Downloaded PBF is unexpectedly small.'
                }
                Move-Item -LiteralPath $downloadPart -Destination $bothPbf -Force
                $downloaded = $true
                break
            } catch {
                Write-PlainText (Join-Path $raw ('download-attempt-' + $attempt + '.log')) (Hide-LocalData $_.Exception.Message)
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds ($attempt * 3)
            }
        }
        if (-not $downloaded) { throw 'Monaco download failed.' }
        Copy-Item -LiteralPath $bothPbf -Destination $vectorPbf
        $report['source_size_bytes'] = (Get-Item -LiteralPath $bothPbf).Length
        $report['source_sha256'] = (Get-FileHash -LiteralPath $bothPbf -Algorithm SHA256).Hash.ToLowerInvariant()
        Write-Check 'monaco_download' 'PASS'

        # Both calls use the actual Windows launcher and the isolated WSL cache.
        $env:OSM2MBTILES_ZOOM = '12-13'
        $env:OSM2MBTILES_BBOX = '7.41,43.72,7.44,43.75'
        $env:OSM2MBTILES_PROCESSES = '2'
        $env:OSM2MBTILES_CLEANUP = 'prune'
        $env:OSM2MBTILES_VECTOR_TILER = 'postgis'
        $env:OSM2MBTILES_VECTOR_IMPORT_PROFILE = 'safe'
        $env:OSM2MBTILES_VECTOR_QUERY_MODE = 'projected'
        $env:OSM2MBTILES_RASTER_FORMAT = 'png'

        $launcher = Join-Path $project 'tools/run_windows.ps1'
        $env:OSM2MBTILES_FORMAT = 'both'
        Write-Host '[RUN] Actual WSL2 launcher: BOTH (Raster PNG + Vector MVT)'
        $exitBoth = Run-Logged 'powershell.exe' @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $launcher, $bothPbf, '12-13'
        ) (Join-Path $raw 'both-run.log')
        if ($exitBoth -ne 0) { throw ('BOTH mode returned exit ' + $exitBoth) }
        Write-Check 'launcher_both' 'PASS'

        # A distinct input filename keeps the vector result from overwriting
        # the vector MBTiles produced by the prior BOTH call.
        $env:OSM2MBTILES_FORMAT = 'vector'
        Write-Host '[RUN] Actual WSL2 launcher: VECTOR (independent PBF import)'
        $exitVector = Run-Logged 'powershell.exe' @(
            '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', $launcher, $vectorPbf, '12-13'
        ) (Join-Path $raw 'vector-run.log')
        if ($exitVector -ne 0) { throw ('VECTOR mode returned exit ' + $exitVector) }
        Write-Check 'launcher_vector' 'PASS'

        $bothRaster = Join-Path $inputs 'monaco-release-both.raster.mbtiles'
        $bothVector = Join-Path $inputs 'monaco-release-both.vector.mbtiles'
        $vectorFile = Join-Path $inputs 'monaco-release-vector.vector.mbtiles'
        $allOutputs = @($bothRaster, $bothVector, $vectorFile)
        foreach ($file in $allOutputs) {
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
                throw ('Expected MBTiles output not created: ' + [IO.Path]::GetFileName($file))
            }
            $fileInfo = Get-Item -LiteralPath $file
            $report['output_files'] += [ordered]@{
                filename = $fileInfo.Name
                bytes = $fileInfo.Length
                sha256 = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }

        $unixScript = Wsl-Path (Join-Path $project 'tools/release_wsl2_verify.py')
        $unixBothRaster = Wsl-Path $bothRaster
        $unixBothVector = Wsl-Path $bothVector
        $unixVector = Wsl-Path $vectorFile
        $unixReport = Wsl-Path (Join-Path $evidence 'mbtiles-proof.json')
        $unixPreview = Wsl-Path (Join-Path $evidence 'preview-raster.png')
        # One genuine BOTH raster and two independent vector outputs.
        $verifyCode = Run-Logged 'wsl.exe' @(
            '--exec', '/usr/bin/python3', $unixScript, $unixBothRaster,
            $unixBothVector, $unixVector, $unixReport, $unixPreview
        ) (Join-Path $raw 'mbtiles-verify.log')
        if ($verifyCode -ne 0) { throw ('MBTiles verification returned exit ' + $verifyCode) }
        $proof = Get-Content -LiteralPath (Join-Path $evidence 'mbtiles-proof.json') -Raw | ConvertFrom-Json
        if (-not $proof.passed) { throw 'MBTiles content verification failed.' }
        Write-Check 'output_validation' 'PASS'
        $report['result'] = 'PASS'
        $zipExit = 0
    }
} catch {
    $report['failure'] = Hide-LocalData $_.Exception.Message
    if ($report['result'] -ne 'SIMULATED_FAIL') { $report['result'] = 'FAIL' }
    Write-Host ('[FAIL] ' + $report['failure']) -ForegroundColor Red
    $zipExit = 1
} finally {
    $report['completed_utc'] = (Get-Date).ToUniversalTime().ToString('o')
    $zipFailure = $false
    try {
        Copy-SafeLogs
        $status = $report['result']
        $safeError = Hide-LocalData ([string]$report['failure'])
        $report['failure'] = if ($safeError) { $safeError } else { $null }
        Write-PlainText (Join-Path $evidence 'summary.json') ($report | ConvertTo-Json -Depth 12)
        $lines = @(
            '# osm2mbtiles - Windows + WSL2 local release E2E'
            ''
            ('Result: ' + $status)
            ('Run ID: ' + $id)
            ('Mode: ' + $report['mode'])
            ('Source PBF SHA256: ' + $report['source_sha256'])
            ''
            'Checks:'
        )
        foreach ($name in $report['steps'].Keys) {
            $lines += ('- ' + $name + ': ' + $report['steps'][$name])
        }
        if ($safeError) { $lines += ('- Error: ' + $safeError) }
        $lines += @(
            ''
            'Scope: actual Windows wrapper -> default Ubuntu/Debian WSL2 -> real BOTH and standalone VECTOR;'
            'real Monaco PBF; SQLite integrity, gzip MVT, nonblank PNG preview, hashes.'
            'Archive self-tests DO NOT run real WSL and must never be called E2E.'
            ''
            'Privacy: no raw PBF, DB, complete MBTiles or raw logs in this ZIP.'
            'Review redacted logs before sharing. Raw logs and MBTiles stay on your PC.'
        )
        Write-PlainText (Join-Path $evidence 'SUMMARY.md') ($lines -join [Environment]::NewLine)
        $provenance = @(
            'Files used by the one-click test:'
            'run.bat'
            'tools/run_windows.ps1'
            'tools/run_wsl.sh'
            'tools/cache_layout.sh'
            'tools/release_wsl2_test.ps1'
            'tools/release_wsl2_verify.py'
            ''
            'Only SHA256 file hashes and technical diagnostics are in this archive.'
            'Map data (Monaco): OpenStreetMap contributors, ODbL.'
            'OpenStreetMap attribution: https://www.openstreetmap.org/copyright'
            'There is no automatic upload or remote collection.'
        )
        Write-PlainText (Join-Path $evidence 'ABOUT.txt') ($provenance -join [Environment]::NewLine)
        $manifest = @()
        foreach ($file in @(Get-ChildItem -LiteralPath $evidence -File -Recurse | Sort-Object FullName)) {
            $relative = $file.FullName.Substring($evidence.Length + 1).Replace('\','/')
            $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            $manifest += ($hash + '  ' + $relative)
        }
        Write-PlainText (Join-Path $evidence 'evidence-sha256.txt') ($manifest -join [Environment]::NewLine)
        Compress-Archive -Path (Join-Path $evidence '*') -DestinationPath $archive -Force -CompressionLevel Optimal
        if (-not (Test-Path -LiteralPath $archive)) { throw 'ZIP was not created.' }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zipRead = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            if ($zipRead.Entries.Count -lt 4) { throw 'ZIP is unexpectedly empty.' }
        } finally {
            $zipRead.Dispose()
        }
        Write-Host ''
        Write-Host ('[ZIP] ' + $archive) -ForegroundColor Cyan
        Write-Host ('[RESULT] ' + $status)
        Write-Host '[NOTE] ZIP has redacted evidence only; original data stays in the test folder.'
    } catch {
        Write-Host ('[ZIP ERROR] ' + $_.Exception.Message) -ForegroundColor Red
        $zipFailure = $true
    }
    if ($zipFailure) { $zipExit = 2 }
}
exit $zipExit
