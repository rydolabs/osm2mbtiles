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
        # IMPORTANT: PowerShell functions emit every Success-stream object.
        # Tee-Object without a terminating sink leaks child output alongside
        # the integer exit code, causing [int] parameter binding to receive
        # System.Object[]. Out-Host preserves live output but emits no value.
        & $Command @Arguments 2>&1 |
            Tee-Object -FilePath $LogPath |
            Out-Host
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

function Assert-WslPreflight([string]$Text, [int]$ExitCode) {
    if ($ExitCode -ne 0) {
        throw ('Default WSL distribution could not execute bash and apt-get. Exit code: ' +
               $ExitCode + '. Review logs/wsl2-preflight.log and logs/wsl-distros.log in the ZIP.')
    }
    if ($Text -notmatch '(?mi)^DISTRO=(ubuntu|debian)\s*$') {
        throw 'Default Linux distribution is not Ubuntu/Debian. Run wsl --list --verbose and set Ubuntu/Debian as default.'
    }
    if ($Text -notmatch '(?mi)^KERNEL=.*microsoft.*wsl2\s*$') {
        throw 'Default distribution does not report a WSL2 kernel. Run wsl --list --verbose and convert that distro to VERSION 2.'
    }
    if ($Text -notmatch '(?mi)^APT_READY\s*$') {
        throw 'The default WSL distribution does not have apt-get; Ubuntu/Debian is required.'
    }
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
        # Synthetic classifier coverage: this NEVER boots or claims WSL2.
        $valid = "DISTRO=ubuntu" + [Environment]::NewLine +
                 "KERNEL=6.6.87.2-microsoft-standard-WSL2" + [Environment]::NewLine +
                 "APT_READY" + [Environment]::NewLine
        Assert-WslPreflight $valid 0
        # Regression for the actual WSL failure on a user's PC:
        # invoke the very same native-process logger used by the real preflight.
        # This is a Windows child-process mock, NOT a real WSL success.
        $childCommand = '[Console]::Out.WriteLine("DISTRO=ubuntu"); [Console]::Out.WriteLine("KERNEL=6.6.87.2-microsoft-standard-WSL2"); [Console]::Out.WriteLine("APT_READY"); [Console]::Error.WriteLine("diagnostic stderr line"); exit 0'
        $mockLog = Join-Path $raw 'mock-wsl-child-success.log'
        $mockExit = Invoke-Logged 'powershell.exe' @('-NoLogo', '-NoProfile', '-Command', $childCommand) $mockLog
        if ($mockExit -isnot [int] -or $mockExit -ne 0) {
            throw ('Native child logger must emit exactly one integer exit code; got ' +
                   $mockExit.GetType().FullName)
        }
        $mockText = Get-Content -LiteralPath $mockLog -Raw
        Assert-WslPreflight $mockText $mockExit
        if ($mockText -notmatch 'diagnostic stderr line') {
            throw 'Native child stderr was not recorded in the log.'
        }
        $badChildCommand = '[Console]::Out.WriteLine("mocked WSL failed"); [Console]::Error.WriteLine("mocked stderr"); exit 7'
        $badLog = Join-Path $raw 'mock-wsl-child-failure.log'
        $badExit = Invoke-Logged 'powershell.exe' @('-NoLogo', '-NoProfile', '-Command', $badChildCommand) $badLog
        if ($badExit -isnot [int] -or $badExit -ne 7) {
            throw 'Native child nonzero exit code was lost or mixed with process output.'
        }
        $nativeFailureRejected = $false
        try { Assert-WslPreflight (Get-Content -LiteralPath $badLog -Raw) $badExit }
        catch { $nativeFailureRejected = $true }
        if (-not $nativeFailureRejected) {
            throw 'Native child preflight falsely accepted a failed process.'
        }
        $quietLog = Join-Path $raw 'mock-wsl-child-quiet.log'
        $quietExit = Invoke-Logged 'powershell.exe' @('-NoLogo', '-NoProfile', '-Command', 'exit 0') $quietLog
        if ($quietExit -isnot [int] -or $quietExit -ne 0) {
            throw 'Quiet native process did not return exactly one integer zero.'
        }
        Write-Host '[CHECK] Windows child stdout+stderr, quiet, and failing exit code are strictly typed (NOT real WSL).'
        foreach ($bad in @(
            @{Text=("DISTRO=ubuntu" + [Environment]::NewLine + "KERNEL=5.10-microsoft-standard" + [Environment]::NewLine + "APT_READY"); Code=0},
            @{Text=("DISTRO=other" + [Environment]::NewLine + "KERNEL=6.6-microsoft-standard-WSL2" + [Environment]::NewLine + "APT_READY"); Code=0},
            @{Text=("DISTRO=ubuntu" + [Environment]::NewLine + "KERNEL=6.6-microsoft-standard-WSL2"); Code=0},
            @{Text=$valid; Code=1}
        )) {
            $rejected = $false
            try { Assert-WslPreflight $bad.Text $bad.Code }
            catch { $rejected = $true }
            if (-not $rejected) { throw 'Invalid WSL preflight fixture was accepted.' }
        }
        Write-Host '[CHECK] Synthetic WSL2 distro/kernel/apt classification PASS (NOT a real WSL run).'
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
        # Record installed distributions for diagnosis, without treating the
        # localized, UTF-16 wsl.exe list output as a WSL2 success guarantee.
        # The previous QA code required WSLENV variable forwarding; it could
        # fail even on a working WSL2 machine. Private cache ID is now passed
        # explicitly by run_windows.ps1 to /usr/bin/env inside WSL.
        $env:OSM2MBTILES_TEST_RUN_ID = $runId
        $oldPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & wsl.exe --list --verbose 2>&1 |
                Out-File -FilePath (Join-Path $raw 'wsl-distros.log') -Encoding Unicode
        } finally {
            $ErrorActionPreference = $oldPreference
        }
        $probe = 'set -e; . /etc/os-release; echo DISTRO=$ID; echo KERNEL=$(uname -r); command -v apt-get >/dev/null; echo APT_READY'
        $probeExit = Invoke-Logged 'wsl.exe' @('--exec', 'bash', '-c', $probe) (Join-Path $raw 'wsl2-preflight.log')
        $probeText = Get-Content -LiteralPath (Join-Path $raw 'wsl2-preflight.log') -Raw
        Assert-WslPreflight $probeText $probeExit
        Write-Host '[CHECK] Default Ubuntu/Debian Linux kernel confirms WSL2.'
        Write-Host '[CHECK] Private test cache ID will be passed explicitly; WSLENV forwarding is not required.'
        $state['wsl_probe'] = Redact $probeText.Trim()
        Stage 'wsl2' 'PASS'

        # Some native packages require sudo in Ubuntu. Ask through the console
        # before starting the fully live piped launcher.
        $uid = Invoke-Logged 'wsl.exe' @('--exec', 'id', '-u') (Join-Path $raw 'wsl-uid.log')
        if ($uid -ne 0) { throw 'WSL user ID check failed.' }
        $uidText = (Get-Content -LiteralPath (Join-Path $raw 'wsl-uid.log') -Raw).Trim()
        if ($uidText -ne '0') {
            Write-Host '[INFO] Ubuntu sudo may prompt for the Ubuntu password (not the Windows password).'
            # Native sudo must inherit the real console: piping through
            # Tee-Object makes password prompts fail on some WSL terminals.
            $prior = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & wsl.exe --exec sudo -v
                $sudoExit = $LASTEXITCODE
            } finally {
                $ErrorActionPreference = $prior
            }
            Write-Utf8 (Join-Path $raw 'wsl-sudo.log') ('sudo preflight exit=' + $sudoExit)
            if ($sudoExit -ne 0) { throw 'Ubuntu sudo permission check failed; confirm your Linux user can run sudo.' }
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
