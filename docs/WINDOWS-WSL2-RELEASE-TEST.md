# Windows + WSL2: one-click release E2E + diagnostic ZIP

## Run the real test on your Windows PC

This is **not** an Actions runner simulation. Run on a Windows 10/11 computer with a **default Ubuntu/Debian WSL2 distribution**. PowerShell 5.1 is included with Windows. WSL Ubuntu needs working `sudo` access and internet access for `apt`, Geofabrik, OSM Carto and its external map data.

1. Download the **whole repository as ZIP** from the [candidate branch](https://github.com/rydolabs/osm2mbtiles/archive/refs/heads/test/release-e2e-gates-20261103.zip) and extract it to a local Windows folder (for example `C:\osm2mbtiles-release-test`). Running only the BAT without the source checkout will not work.
2. Confirm Ubuntu is WSL **version 2** and is the default: `wsl --list --verbose`. If necessary, use `wsl --set-default Ubuntu` and `wsl --set-version Ubuntu 2` (substitute the installed distribution name).
3. **Double-click `TEST-Windows-WSL2.bat`** in the extracted root. No PBF selection or extra CLI arguments are needed. If Ubuntu asks for your sudo password to install its native dependencies, enter the **Ubuntu account password**, not your Windows password.
4. After completion, open `release-test-results\` next to the BAT, and find the latest `qa_*.zip`. Attach **that ZIP only** for analysis after reviewing its contents. You can also inspect `summary.json` or `SUMMARY.md` inside.

### What it actually runs

- Checks the default Ubuntu/Debian WSL2 kernel, `apt-get` and isolated test cache forwarding. Tests **fail closed** if WSL is missing or reports WSL1.
- Downloads a small **real Geofabrik Monaco OSM PBF** over HTTPS, records SHA-256, and copies it to a second, uniquely named input (avoids overwriting a previous Vector MBTiles output).
- Executes the project's actual `tools/run_windows.ps1` → `tools/run_wsl.sh` launch path, first with `OSM2MBTILES_FORMAT=both`, then with `vector`, bounded to Monaco bbox `7.41,43.72,7.44,43.75` and zoom `12-13`. These invoke the real PostGIS/Mapnik/OSM Carto pipeline.
- Checks **both-mode PNG Raster**, **both-mode gzip MVT Vector**, and **independent Vector-only archive** for SQLite integrity, format, tile coordinates, non-empty gzip content, vector layer metadata, and nonblank PNG cartography. Saves a **real Raster PNG tile preview**.
- Generates a ZIP even on failures, including a stage-by-stage verdict and redacted logs. **No automatic uploads.**

A successful real test prints `[RESULT] PASS`. If it fails, the ZIP includes the failed stage and sanitized diagnostics; a ZIP being created does **not** mean the test passed.

### Evidence and privacy

The ZIP contains `summary.json`, `SUMMARY.md`, `mbtiles-proof.json` when generation succeeds, `preview-raster.png`, `evidence-sha256.txt` and sanitized `logs/`. It deliberately excludes the original input PBF, the full MBTiles archives, all PostgreSQL files and the unredacted raw logs. **Review the ZIP before sharing:** automatic redaction is best-effort and cannot guarantee that all sensitive strings have been removed.

The local run folder contains `inputs-and-outputs/` (actual archives and PBF), `raw-logs/` (unredacted), and `zip-evidence/`. These remain **on the PC** for debugging; do not share `raw-logs/`. The ZIP file is a sibling of the run folder, under `release-test-results`.

The harness sets a temporary `OSM2MBTILES_TEST_RUN_ID` and passes it with `WSLENV`. Its WSL cache uses `~/.cache/osm2mbtiles-release-tests/<run-id>` instead of the normal `~/.cache/osm2mbtiles` conversion cache. The `prune` cleanup policy removes the private PostgreSQL database after successful copied outputs, but downloaded style assets may remain in the isolated test cache. To reclaim that cache, inspect its location and remove **only** the matching test run ID, not your normal cache.

### Limits

- No full South Korea / Planet-scale workload and no automatic replication feed is used. The previous Actions runner already covers three `.osc.gz` diffs and independent full-rebuild parity; this test concentrates on **real Windows + WSL2 launch and copy**.
- A first run can install PostgreSQL, osm2pgsql, Mapnik, Carto, fonts and GIS assets **inside Ubuntu WSL**, and download external files. It needs sufficient WSL filesystem disk, memory and network access; it does not install packages globally on Windows.
- No MapLibre/browser preview or Windows native Mapnik path is tested.
- `-ArchiveSelfTest` and `-ArchiveSelfTestFailure` are **CI-only** report/ZIP plumbing tests, do **not** execute WSL, and can **never** establish Windows+WSL2 E2E success.

The BAT, PowerShell and Python verifier are plain text and can be inspected before execution. The candidate source is part of [Draft PR #32](https://github.com/rydolabs/osm2mbtiles/pull/32); it is not yet a stable release.
