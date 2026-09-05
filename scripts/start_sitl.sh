#!/usr/bin/env bash
# Start ArduPilot SITL for MavlinkController integration tests.
# Requires: ardupilot installed or Docker.
#
# Usage:
#   ./scripts/start_sitl.sh          # tries Docker first, then native
#   ./scripts/start_sitl.sh --docker # force Docker
#   ./scripts/start_sitl.sh --native # force native sim_vehicle.py

set -euo pipefail

VEHICLE="ArduCopter"
# SITL defaults: MAVSDK connects on udp://:14540
# SITL listens on tcp:5760, mavproxy forwards to 14540

if [[ "${1:-}" == "--native" ]]; then
    if ! command -v sim_vehicle.py &>/dev/null; then
        echo "ERROR: sim_vehicle.py not found on PATH."
        echo "Install ArduPilot SITL: https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html"
        exit 1
    fi
    echo "Starting native SITL..."
    sim_vehicle.py -v $VEHICLE --no-rebuild -w \
        --out=udp:127.0.0.1:14540
elif [[ "${1:-}" == "--docker" ]] || command -v docker &>/dev/null; then
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
    docker rm -f flightrisk-sitl >/dev/null 2>&1 || true

    echo "Starting SITL via Docker..."
    # NOTE: radarku/ardupilot-sitl is pinned to :latest -- no versioned
    # tag is published upstream at time of writing. Pin to a digest if
    # reproducibility becomes an issue.
    docker run --rm -d \
        --name flightrisk-sitl \
        --add-host=host.docker.internal:host-gateway \
        -p 5760:5760 \
        -p 14540:14540/udp \
        radarku/ardupilot-sitl:latest \
        sim_vehicle.py -v $VEHICLE --no-rebuild -w \
        --out=udpout:host.docker.internal:14540
    echo "SITL running in Docker. Connect on udp://:14540"
    echo "Stop with: docker stop flightrisk-sitl"
elif command -v sim_vehicle.py &>/dev/null; then
    echo "Starting native SITL..."
    sim_vehicle.py -v $VEHICLE --no-rebuild -w \
        --out=udp:127.0.0.1:14540
else
    echo "ERROR: Neither Docker nor sim_vehicle.py found."
    echo "Install ArduPilot SITL or Docker to run integration tests."
    exit 1
fi
