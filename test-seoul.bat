@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title osm2mbtiles - Seoul PNG and Vector PBF smoke generation
rem Windows 10/11 + Ubuntu/Debian WSL2 required, no Docker.
rem No arguments: test-seoul.bat => both z12-14.
rem Optional: test-seoul.bat 12-15 vector
rem Results: test-output\seoul-non-military.raster.mbtiles and .vector.mbtiles
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Windows PowerShell is required.
  exit /b 3
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\test_seoul.ps1" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo [ERROR] Seoul generation failed with exit code %RESULT%.
  exit /b %RESULT%
)
echo [OK] Seoul generation and output verification passed.
exit /b 0
