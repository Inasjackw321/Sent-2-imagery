#!/bin/bash
# macOS / Linux: double-click to launch Sent-2.
# Creates a local virtual environment on first run, then opens the app window.

cd "$(dirname "$0")/.." || exit 1

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/"
  read -r -p "Press return to close."
  exit 1
fi

VENV=".venv"
STAMP="$VENV/.deps-installed"

if [ ! -d "$VENV" ]; then
  echo "Setting up (first run only)…"
  "$PY" -m venv "$VENV" || { echo "Could not create $VENV"; read -r; exit 1; }
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "Installing dependencies…"
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || { echo "Install failed"; read -r; exit 1; }
  touch "$STAMP"
fi

exec python app.py "$@"
