@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title osm2mbtiles - Real Windows WSL2 Progress Test + Evidence ZIP

rem Double-click this file from a complete checkout of the progress branch.
rem Downloads small Geofabrik Monaco PBF, runs real Mapnik Raster + PostGIS
rem Vector through the normal Windows/WSL one-click launcher. Always tries
rem to save a diagnostic ZIP under progress-test-results, even on failure.
rem No automatic upload. Local PBF, MBTiles and raw logs are NOT in the ZIP.

if not exist "%~dp0tools\progress_wsl2_test.ps1" (
  echo [ERROR] Extract the WHOLE source archive first.
  echo [ERROR] tools\progress_wsl2_test.ps1 is missing.
  pause
  exit /b 3
)
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell 5.1 is required.
  pause
  exit /b 3
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\progress_wsl2_test.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo [FAIL] Real WSL2 test failed, or evidence ZIP creation failed.
) else (
  echo [PASS] Check summary.json in the ZIP: real pass requires mode=real_windows_wsl2.
)
echo [INFO] Results: "%~dp0progress-test-results"
echo [INFO] Share only the redacted qa_*.zip after reviewing it.
pause
exit /b %RESULT%
