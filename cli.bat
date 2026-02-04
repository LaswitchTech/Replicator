@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM ---------------------------------------------------------------------------
REM Replicator CLI launcher (Windows)
REM - Ensures a local venv exists (.venv)
REM - Installs minimal runtime deps (PyQt5)
REM - Runs src\main.py with a visible console
REM
REM Note: If you double-click this file, the console may flash and close.
REM       Run it from an existing Command Prompt for persistent output.
REM ---------------------------------------------------------------------------

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

REM Prefer Windows Python Launcher
set "PY_CMD="
set "PY_ARGS="
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  set "PY_CMD=py"
  set "PY_ARGS=-3.11"
) else (
  where python >nul 2>&1
  if %ERRORLEVEL%==0 (
    set "PY_CMD=python"
    set "PY_ARGS="
  )
)

if "%PY_CMD%"=="" (
  echo ERROR: Python not found. Install Python 3.11+ and ensure either `py` or `python` is available in PATH.
  exit /b 1
)

REM Create venv if missing
if not exist "%VENV_PY%" (
  echo Creating virtualenv: %VENV_DIR%
  %PY_CMD% %PY_ARGS% -m venv "%VENV_DIR%"
  if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to create virtualenv.
    exit /b %ERRORLEVEL%
  )
)

REM Upgrade pip/wheel
"%VENV_PY%" -m pip install --upgrade pip wheel >nul

REM Install minimal deps (idempotent)
echo Installing minimal runtime deps (PyQt5)...
"%VENV_PY%" -m pip install "PyQt5>=5.15,<6" >nul

REM Run the app entry in console mode
if not exist "%SCRIPT_DIR%src\main.py" (
  echo ERROR: Entry not found: %SCRIPT_DIR%src\main.py
  exit /b 1
)

"%VENV_PY%" "%SCRIPT_DIR%src\main.py" %*
endlocal
exit /b %ERRORLEVEL%
