#!/usr/bin/env bash
# Start one FlightRisk edge process that flies one Tello (run on the Mac for drone 1).
#
#   export FLIGHTRISK_FLEET_NET=station   # or: ap  (see docs/fleet-setup.md)
#   export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"
#   DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
#
# DRONE_ID is 1 or 2 (or a full id such as drone-2) and becomes the fleet id drone-1 or
# drone-2, which the ground station's --fleet list must contain. Default: 1.
# ALT_CM is this drone's altitude slot in cm (at least 10). Default: 100 for odd ids and
# 160 for even ids, the same alternation the coordinator uses.
# The Tello address and ground URL come from scripts/fleet_env.sh. In ap mode, set
# FLIGHTRISK_GROUND_LOCAL=1 on the Mac (which runs the ground station) so edge-1 connects to
# ws://127.0.0.1:9000 and keeps flying if the wired cable is pulled; without it the edge
# dials ws://10.42.0.1:9000, which is what the Windows laptop must do.
# DEMO_DRY_RUN=1 prints the command instead of running it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/fleet_env.sh
source scripts/fleet_env.sh || exit 1

if [ -z "${FLIGHTRISK_EDGE_TOKEN:-}" ]; then
    cat >&2 <<'MSG'
demo_edge.sh: FLIGHTRISK_EDGE_TOKEN is not set, so the edge cannot authenticate to the ground station.
Use the same secret as on the ground station:

    export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"
MSG
    exit 1
fi

raw_id="${DRONE_ID:-1}"
case "$raw_id" in
    drone-[0-9]*) number="${raw_id#drone-}" ;;
    *) number="$raw_id" ;;
esac
case "$number" in
    ''|*[!0-9]*)
        echo "demo_edge.sh: DRONE_ID must be a number such as 1 or 2 (got '$raw_id')" >&2
        exit 1
        ;;
esac
number=$((10#$number))
drone_id="drone-$number"

if [ -z "${ALT_CM:-}" ]; then
    if [ $((number % 2)) -eq 1 ]; then ALT_CM=100; else ALT_CM=160; fi
fi
case "$ALT_CM" in
    ''|*[!0-9]*)
        echo "demo_edge.sh: ALT_CM must be a whole number of centimetres (got '$ALT_CM')" >&2
        exit 1
        ;;
esac
if [ "$ALT_CM" -lt 10 ]; then
    echo "demo_edge.sh: ALT_CM must be at least 10; smaller values are read as metres" >&2
    exit 1
fi

PYTHON="${PYTHON:-venv/bin/python}"

# The live floor is 4 m x 4 m, so the geofence is 450 cm unless the environment overrides it,
# and the edge matches on appearance only: it builds no face recognizer and sends no face embeddings.
export FLIGHTRISK_GEOFENCE_CM="${FLIGHTRISK_GEOFENCE_CM:-450}"
export FLIGHTRISK_APPEARANCE_ONLY=1

cmd=("$PYTHON" -m flightrisk.edge_device
    --source tello
    --tello-host "$FLIGHTRISK_TELLO_HOST"
    --drone-id "$drone_id"
    --alt-slot "$ALT_CM"
    --ws-url "$FLIGHTRISK_EDGE_WS")

if [ -n "${DEMO_DRY_RUN:-}" ]; then
    echo "${cmd[*]}"
    exit 0
fi
exec "${cmd[@]}"
