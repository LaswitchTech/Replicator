@echo off
setlocal

REM Replicator CLI launcher (Windows)
REM - Runs the built EXE if present
REM - If not present, asks the user to run ./cli.sh from Git Bash

set "SCRIPT_DIR=%~dp0"

if exist "%SCRIPT_DIR%dist\\windows\\Replicator.exe" (
  "%SCRIPT_DIR%dist\\windows\\Replicator.exe" %*
  endlocal
  exit /b %ERRORLEVEL%
)

echo Replicator.exe not found at "%SCRIPT_DIR%dist\\windows\\Replicator.exe".
echo If you are in a dev checkout, use Git Bash and run: ./cli.sh --help
endlocal
exit /b 1
