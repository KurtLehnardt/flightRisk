#!/usr/bin/env bash
# Source this file (do not execute it) to export the fleet network settings.
#
#   export FLIGHTRISK_FLEET_NET=station   # or: ap
#   source scripts/fleet_env.sh
#
# station mode needs FLIGHTRISK_GROUND_IP (the Mac's LAN IP) and FLIGHTRISK_TELLO_HOST
# (the IP of the Tello that this host controls, from the discovery step).
# ap mode needs nothing else: the addresses are fixed by the wired-link plan.
# Both modes need FLIGHTRISK_EDGE_TOKEN to be set, because the ground station binds
# a non-loopback address.

_fleet_fail() {
    echo "fleet_env.sh: $1" >&2
    return 1 2>/dev/null || exit 1
}

case "${FLIGHTRISK_FLEET_NET:-}" in
    station)
        [ -n "${FLIGHTRISK_GROUND_IP:-}" ] || { _fleet_fail "station mode needs FLIGHTRISK_GROUND_IP (the Mac LAN IP)"; return 1 2>/dev/null || exit 1; }
        [ -n "${FLIGHTRISK_TELLO_HOST:-}" ] || { _fleet_fail "station mode needs FLIGHTRISK_TELLO_HOST (the discovered Tello IP for this host)"; return 1 2>/dev/null || exit 1; }
        export FLIGHTRISK_EDGE_HOST="$FLIGHTRISK_GROUND_IP"
        export FLIGHTRISK_EDGE_WS="ws://$FLIGHTRISK_GROUND_IP:9000"
        ;;
    ap)
        export FLIGHTRISK_GROUND_IP="10.42.0.1"
        export FLIGHTRISK_TELLO_HOST="192.168.10.1"
        export FLIGHTRISK_EDGE_HOST="10.42.0.1"
        export FLIGHTRISK_EDGE_WS="ws://10.42.0.1:9000"
        ;;
    *)
        _fleet_fail "set FLIGHTRISK_FLEET_NET to station or ap (got '${FLIGHTRISK_FLEET_NET:-}')"
        return 1 2>/dev/null || exit 1
        ;;
esac

if [ -z "${FLIGHTRISK_EDGE_TOKEN:-}" ]; then
    echo "fleet_env.sh: warning: FLIGHTRISK_EDGE_TOKEN is not set; the ground station refuses to start without it." >&2
fi

echo "fleet_env.sh: FLIGHTRISK_FLEET_NET=$FLIGHTRISK_FLEET_NET edge-host=$FLIGHTRISK_EDGE_HOST tello-host=$FLIGHTRISK_TELLO_HOST ws=$FLIGHTRISK_EDGE_WS"
