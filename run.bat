@echo off
setlocal

REM Replicator double-click launcher (Windows)
REM - Runs PowerShell wrapper with a hidden window
REM - Exits immediately so no console stays open

set "SCRIPT_DIR=%~dp0"

REM Use Start-Process so this batch exits right away.
REM WindowStyle Hidden avoids showing a console window.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -WindowStyle Hidden -FilePath 'powershell' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', (Join-Path '%SCRIPT_DIR%' 'run.ps1'))"

endlocal
exit /b 0
