#!/usr/bin/env bash
set -euo pipefail

# Replicator dev launcher (Git Bash compatible)
# - Creates a virtualenv if missing
# - Installs runtime deps (prefers requirements.txt if present)
# - Runs src/main.py

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_DIR=".venv"
REQ_FILE="requirements.txt"
PY_BIN="python3.11"

if command -v "$PY_BIN" >/dev/null 2>&1; then
  :
elif command -v python3 >/dev/null 2>&1; then
  PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PY_BIN="python"
else
  echo "ERROR: Python not found in PATH" >&2
  exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating virtualenv: $VENV_DIR"
  "$PY_BIN" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip wheel

if [ -f "$REQ_FILE" ]; then
  echo "Installing requirements from $REQ_FILE"
  python -m pip install -r "$REQ_FILE"
else
  echo "Installing minimal runtime deps (PyQt5)"
  python -m pip install "PyQt5>=5.15,<6"
fi

exec python "src/main.py" "$@"
