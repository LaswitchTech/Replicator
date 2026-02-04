@echo off
setlocal

REM Replicator CLI wrapper (Windows)
REM Always runs in the current console and forwards all arguments.

set "SCRIPT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run.ps1" %*

endlocal
exit /b %ERRORLEVEL%
