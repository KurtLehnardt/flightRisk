#!/usr/bin/env bash
# Open the FlightRisk launcher window (macOS: double-click this file in Finder).
#
# It runs the launcher with the repository's virtual environment, serves the window on
# http://127.0.0.1:5500/ and opens it in the browser. The edge token is read by the launcher
# itself from FLIGHTRISK_EDGE_TOKEN or ~/.flightrisk-fleet.env. Extra arguments are passed on,
# for example --port 5501 or --no-browser. See docs/launcher.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    echo "launcher.command: $ROOT/$PYTHON was not found. Create the virtual environment first (README, Quick Start)." >&2
    read -r -p "Press Enter to close this window. " _ || true
    exit 1
fi

status=0
"$PYTHON" -m flightrisk.launcher "$@" || status=$?
if [ "$status" -ne 0 ]; then
    read -r -p "The launcher exited with code $status. Press Enter to close this window. " _ || true
fi
exit "$status"
