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
    echo "Starting SITL via Docker..."
    docker run --rm -d \
        --name amber-sitl \
        -p 5760:5760 \
        -p 14540:14540/udp \
        radarku/ardupilot-sitl:latest \
        sim_vehicle.py -v $VEHICLE --no-rebuild -w \
        --out=udpout:host.docker.internal:14540
    echo "SITL running in Docker. Connect on udp://:14540"
    echo "Stop with: docker stop amber-sitl"
elif command -v sim_vehicle.py &>/dev/null; then
    echo "Starting native SITL..."
    sim_vehicle.py -v $VEHICLE --no-rebuild -w \
        --out=udp:127.0.0.1:14540
else
    echo "ERROR: Neither Docker nor sim_vehicle.py found."
    echo "Install ArduPilot SITL or Docker to run integration tests."
    exit 1
fi
