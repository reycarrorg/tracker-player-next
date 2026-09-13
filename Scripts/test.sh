#!/bin/sh
set -eu
PYTHON_BIN=${1:-python3}
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(pwd)/Engine:$(pwd)/Tests" "$PYTHON_BIN" -m unittest discover -s Tests -p 'test_*.py' -v
"$PYTHON_BIN" Scripts/check_public_boundary.py
