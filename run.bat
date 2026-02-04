@echo off
setlocal

REM Convenience launcher for PowerShell wrapper
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*

endlocal
