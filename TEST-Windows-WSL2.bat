@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title osm2mbtiles - Windows WSL2 Release E2E and Evidence ZIP

rem Double-click after extracting the repository ZIP on your Windows PC.
rem No user PBF needed: a tiny Geofabrik Monaco extract is downloaded.
rem Actual WSL2 Ubuntu: BOTH (PNG + MVT) and separate VECTOR conversion.
rem Always writes a redacted evidence ZIP under release-test-results.
rem Keeps raw logs, input PBF, and complete MBTiles locally; uploads nothing.

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell 5.1 or newer is required.
  pause
  exit /b 3
)
if not exist "%~dp0tools\release_wsl2_test.ps1" (
  echo [ERROR] Extract the whole repository. tools\release_wsl2_test.ps1 was not found.
  pause
  exit /b 3
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\release_wsl2_test.ps1" %*
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" (
  echo [FAIL] E2E did not pass. A diagnostic ZIP should still have been generated.
) else (
  echo [PASS] E2E completed or the explicit archive-only self-test completed.
)
echo [INFO] Check release-test-results for the most recent .zip report.
echo [INFO] For a real test, do not use -ArchiveSelfTest.
pause
exit /b %RESULT%
