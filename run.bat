@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title osm2mbtiles - Docker-free Raster and Vector MBTiles

rem Double-click: auto-download South Korea PBF and write south-korea-latest.mbtiles.
rem Optional: drag a *.osm.pbf file onto run.bat to use a different input.
rem Optional: run.bat 0-12 to override zoom for automatic South Korea conversion.
rem Optional: run.bat vector [0-15] or run.bat both [0-15].
rem Or set OSM2MBTILES_FORMAT=vector|raster|both (default raster).
rem Optional environment settings: OSM2MBTILES_ZOOM, OSM2MBTILES_PROCESSES
rem Cache cleanup after successful output verification and Windows copy:
rem   compact (default): delete duplicate WSL MBTiles, keep PBF + DB for reruns
rem   prune: remove duplicate MBTiles + private PostgreSQL DB (slow next run)
rem   keep: keep everything including duplicate WSL MBTiles
rem Example command prompt: set "OSM2MBTILES_CLEANUP=prune" && run.bat
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell is required.
  pause
  exit /b 1
)

if /i "%~1"=="--tile-type" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_windows.ps1" "%~2" "%~3"
) else if /i "%~1"=="--format" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_windows.ps1" "%~2" "%~3"
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\run_windows.ps1" %*
)
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo [ERROR] Conversion did not finish. Exit code: %RESULT%
  pause
  exit /b %RESULT%
)
echo.
echo [OK] Finished. Press any key to close.
pause >nul
exit /b 0