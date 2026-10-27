#!/usr/bin/env bash
# Start the FlightRisk ground station for the two-drone hackathon demo (run on the Mac).
#
#   export FLIGHTRISK_FLEET_NET=station   # or: ap  (see docs/fleet-setup.md)
#   export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"
#   scripts/demo_ground.sh
#
# The edge WebSocket binds FLIGHTRISK_EDGE_HOST (derived by scripts/fleet_env.sh; in ap mode
# the list "127.0.0.1,10.42.0.1", loopback plus the wired link) and needs FLIGHTRISK_EDGE_TOKEN. The dashboard itself stays on 127.0.0.1: open
# http://127.0.0.1:5555 in a browser on this Mac. The demo runs appearance-only, writes
# no match snapshots and starts no reasoning model.
#
# Optional: FLIGHTRISK_FLEET_DRONES (default drone-1,drone-2; these must match the drone
# ids the edges use), FLIGHTRISK_FLEET_AREA_CM, FLIGHTRISK_FLEET_STRIP_CM, PYTHON.
# DEMO_DRY_RUN=1 prints the command instead of running it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/fleet_env.sh
source scripts/fleet_env.sh || exit 1

if [ -z "${FLIGHTRISK_EDGE_TOKEN:-}" ]; then
    cat >&2 <<'MSG'
demo_ground.sh: FLIGHTRISK_EDGE_TOKEN is not set, so the ground station will not start.
The edge link listens on the LAN and needs a shared secret. Pick one and use the same
value on every laptop:

    export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"
MSG
    exit 1
fi

PYTHON="${PYTHON:-venv/bin/python}"
export FLIGHTRISK_APPEARANCE_ONLY=1
export FLIGHTRISK_SAVE_CAPTURES=0

cmd=("$PYTHON" -m flightrisk.dashboard
    --source edge
    --edge-host "$FLIGHTRISK_EDGE_HOST"
    --fleet "${FLIGHTRISK_FLEET_DRONES:-drone-1,drone-2}"
    --no-llm)

if [ -n "${DEMO_DRY_RUN:-}" ]; then
    echo "${cmd[*]}"
    exit 0
fi
exec "${cmd[@]}"
