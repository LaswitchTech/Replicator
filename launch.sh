#!/usr/bin/env bash
set -euo pipefail

# Replicator dev launcher (macOS/Linux + Windows Git Bash)
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR=".venv"
REQ_FILE="requirements.txt"

# Detect Windows Git Bash (MSYS/MINGW/CYGWIN)
UNAME_S="$(uname -s 2>/dev/null || echo '')"
IS_WINDOWS=0
case "$UNAME_S" in
  MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;;
  *) IS_WINDOWS=0 ;;
esac

# Choose python command
PY_CMD=""
PY_ARGS=()

# Prefer explicit python3.11 on macOS/Linux
if command -v python3.11 >/dev/null 2>&1; then
  PY_CMD="python3.11"
elif [ "$IS_WINDOWS" -eq 1 ] && command -v py >/dev/null 2>&1; then
  # Prefer Windows Python Launcher (does NOT require python.exe in PATH)
  PY_CMD="py"
  PY_ARGS=(-3.11)
elif command -v python3 >/dev/null 2>&1; then
  PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
  PY_CMD="python"
else
  echo "ERROR: Python not found in PATH. On Windows, install Python 3.11+ and/or ensure the Python Launcher (py) is available." >&2
  exit 1
fi

# Resolve venv python/activate paths
if [ "$IS_WINDOWS" -eq 1 ]; then
  VENV_PY="$VENV_DIR/Scripts/python.exe"
  ACTIVATE_SH="$VENV_DIR/Scripts/activate"
else
  VENV_PY="$VENV_DIR/bin/python"
  ACTIVATE_SH="$VENV_DIR/bin/activate"
fi

# Create venv if needed
if [ ! -x "$VENV_PY" ]; then
  echo "Creating virtualenv: $VENV_DIR"
  # shellcheck disable=SC2086
  "$PY_CMD" "${PY_ARGS[@]}" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck disable=SC1090
source "$ACTIVATE_SH"

python -m pip install --upgrade pip wheel

if [ -f "$REQ_FILE" ]; then
  echo "Installing requirements from $REQ_FILE"
  python -m pip install -r "$REQ_FILE"
else
  echo "Installing minimal runtime deps (PyQt5)"
  python -m pip install "PyQt5>=5.15,<6"
fi

exec python "src/main.py" "$@"
