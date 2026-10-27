#!/usr/bin/env bash
# Start ArduPilot SITL for MavlinkController tests and scripts/sitl_fly_coverage.py.
#
# Usage:
#   ./scripts/start_sitl.sh                       # Docker if available, else native
#   ./scripts/start_sitl.sh --docker              # force Docker
#   ./scripts/start_sitl.sh --native              # force native sim_vehicle.py
#   ./scripts/start_sitl.sh --instance 1          # second vehicle (see ports below)
#   ./scripts/start_sitl.sh --origin 40.6894,-111.7310,1600   # LAT,LON[,ALT_AMSL_M]
#
# Ports: instance N listens for MAVLink on TCP 5760 + 10 * N. The Docker image
# runs without MAVProxy, so connect MAVSDK with tcpout://127.0.0.1:<port>
# (scripts/sitl_fly_coverage.py --instance N does this for you).
#
# The Docker image is amd64 only and pinned by digest. On an arm64 host such as
# an Apple Silicon Mac it runs under emulation, which is slow: expect several
# minutes before the EKF reports a GPS fix and the vehicle is armable.

set -euo pipefail

VEHICLE="ArduCopter"
# Pinned by digest (the :latest tag was last pushed in January 2021).
IMAGE="radarku/ardupilot-sitl@sha256:2364ae14e190e4cdd5d0839b3a55e6b5c6a980d9ff0000b1be7b8baae6fd50f1"
INSTANCE=0
MODE=""
ORIGIN="40.6894,-111.7310,1600"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --native) MODE="native"; shift ;;
        --docker) MODE="docker"; shift ;;
        --instance)
            [[ $# -ge 2 ]] || { echo "ERROR: --instance needs a number."; exit 2; }
            INSTANCE="$2"; shift 2 ;;
        --origin)
            [[ $# -ge 2 ]] || { echo "ERROR: --origin needs LAT,LON[,ALT]."; exit 2; }
            ORIGIN="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "ERROR: unknown argument: $1"; exit 2 ;;
    esac
done

if ! [[ "$INSTANCE" =~ ^[0-9]+$ ]]; then
    echo "ERROR: --instance must be a non-negative integer, got '$INSTANCE'."
    exit 2
fi

IFS=',' read -r LAT LON ALT <<< "$ORIGIN"
ALT="${ALT:-1600}"
if [[ -z "${LAT:-}" || -z "${LON:-}" ]]; then
    echo "ERROR: --origin must look like LAT,LON[,ALT], got '$ORIGIN'."
    exit 2
fi

PORT=$((5760 + 10 * INSTANCE))
NAME="flightrisk-sitl-${INSTANCE}"

start_native() {
    if ! command -v sim_vehicle.py &>/dev/null; then
        echo "ERROR: sim_vehicle.py not found on PATH."
        echo "Install ArduPilot SITL: https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html"
        exit 1
    fi
    echo "Starting native SITL instance ${INSTANCE} (MAVSDK: udp://:$((14540 + INSTANCE)))..."
    exec sim_vehicle.py -v "$VEHICLE" -I "$INSTANCE" --no-rebuild -w \
        --custom-location="${LAT},${LON},${ALT},0" \
        --out="udp:127.0.0.1:$((14540 + INSTANCE))"
}

start_docker() {
    if ! command -v docker &>/dev/null; then
        echo "ERROR: Docker not found."
        exit 1
    fi
    if ! docker info &>/dev/null 2>&1; then
        echo "ERROR: Docker daemon not running."
        exit 1
    fi

    # Remove any stale container from a previous run so this script can
    # be re-run without a "name already in use" failure.
    docker rm -f "$NAME" >/dev/null 2>&1 || true

    echo "Starting SITL instance ${INSTANCE} via Docker (amd64, emulated on arm64 hosts)..."
    docker run --rm -d \
        --platform linux/amd64 \
        --name "$NAME" \
        -e "INSTANCE=${INSTANCE}" \
        -e "LAT=${LAT}" -e "LON=${LON}" -e "ALT=${ALT}" -e "DIR=0" \
        -p "${PORT}:${PORT}" \
        "$IMAGE"
    echo "SITL running in Docker. Connect MAVSDK on tcpout://127.0.0.1:${PORT}"
    echo "Fly it with: python scripts/sitl_fly_coverage.py --instance ${INSTANCE}"
    echo "Stop with: docker stop ${NAME}"
}

if [[ "$MODE" == "native" ]]; then
    start_native
elif [[ "$MODE" == "docker" ]] || command -v docker &>/dev/null; then
    start_docker
elif command -v sim_vehicle.py &>/dev/null; then
    start_native
else
    echo "ERROR: Neither Docker nor sim_vehicle.py found."
    echo "Install ArduPilot SITL or Docker to run integration tests."
    exit 1
fi
