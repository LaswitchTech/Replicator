Param(
  [Parameter(ValueFromRemainingArguments=$true)]
  [string[]]$Args
)

# Replicator dev/run wrapper
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Resolve-Path $ScriptDir
Set-Location $RootDir

$VenvDir = ".venv"
$ReqFile = "requirements.txt"

$Py = "python"
if (Get-Command "python3.11" -ErrorAction SilentlyContinue) { $Py = "python3.11" }
elseif (Get-Command "python" -ErrorAction SilentlyContinue) { $Py = "python" }
else { throw "Python not found in PATH" }

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
  Write-Host "Creating virtualenv: $VenvDir"
  & $Py -m venv $VenvDir
}

& (Join-Path $VenvDir "Scripts\python.exe") -m pip install --upgrade pip wheel

if (Test-Path $ReqFile) {
  Write-Host "Installing requirements from $ReqFile"
  & (Join-Path $VenvDir "Scripts\python.exe") -m pip install -r $ReqFile
} else {
  Write-Host "Installing minimal runtime deps (PyQt5)"
  & (Join-Path $VenvDir "Scripts\python.exe") -m pip install "PyQt5>=5.15,<6"
}

& (Join-Path $VenvDir "Scripts\python.exe") "src\main.py" @Args
