@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title osm2mbtiles - Cleanup

echo.
echo === osm2mbtiles WSL Cleanup ===
echo.
echo  1. Show WSL cache usage (no deletion)
echo  2. Prune imported PostgreSQL DB (keeps cached PBF, native tools, fonts)
echo  3. Exit
echo.
choice /C 123 /N /M "Select [1/2/3]: "
if errorlevel 3 exit /b 0
if errorlevel 2 (
  echo.
  echo [INFO] This deletes the private imported PostGIS DB and any duplicate Korean MBTiles.
  echo [INFO] Re-running run.bat afterwards requires reimporting the PBF.
  choice /C YN /N /M "Confirm DB cleanup [Y/N]: "
  if errorlevel 2 exit /b 0
  set "CACHE_ACTION=prune"
) else (
  set "CACHE_ACTION=status"
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\cache_windows.ps1" -Action %CACHE_ACTION%
set "RESULT=%ERRORLEVEL%"
echo.
if not "%RESULT%"=="0" echo [ERROR] Cache operation failed ^(code %RESULT%^).
if "%RESULT%"=="0" echo [OK] Cache operation finished.
pause
exit /b %RESULT%