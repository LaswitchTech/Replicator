@echo off
setlocal

REM Replicator launcher (Windows)
REM - Double-click (no args): starts hidden and exits immediately
REM - CLI usage (with args like --help): runs in the current console so you can see output

set "SCRIPT_DIR=%~dp0"

REM If the user provided args, run in the current console (do not hide), so output is visible.
if not "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run.ps1" %*
  endlocal
  exit /b %ERRORLEVEL%
)

REM No args: start hidden and exit right away.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -WindowStyle Hidden -FilePath 'powershell' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', (Join-Path '%SCRIPT_DIR%' 'run.ps1'))"

endlocal
exit /b 0
