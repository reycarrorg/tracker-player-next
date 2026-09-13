#!/bin/sh
set -eu
PYTHON_BIN=${1:-python3}
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$(pwd)/Engine:$(pwd)/Tests${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m unittest discover -s Tests -p 'test_*.py' -v
mkdir -p Build
xcrun swiftc App/Navigation.swift Tests/SourceIndicatorChecks.swift -o Build/SourceIndicatorChecks
Build/SourceIndicatorChecks
"$PYTHON_BIN" Scripts/check_public_boundary.py
