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
#
# ap mode binds the ground station to "127.0.0.1,10.42.0.1" (FLIGHTRISK_EDGE_HOST). Set
#   export FLIGHTRISK_GROUND_LOCAL=1
# on the machine that runs the ground station (the Mac, which also flies edge-1) so its
# edge connects to ws://127.0.0.1:9000. Leave it unset on the Windows laptop, whose edge-2
# connects to ws://10.42.0.1:9000. Station mode ignores FLIGHTRISK_GROUND_LOCAL.

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
        # Ground bind list: loopback first, then the wired-link address. Never a wildcard.
        export FLIGHTRISK_EDGE_HOST="127.0.0.1,10.42.0.1"
        # Where an edge connects. FLIGHTRISK_GROUND_LOCAL=1 means the ground station runs on
        # this machine (the Mac, edge-1), so the edge uses loopback and survives the cable
        # being pulled. Otherwise (the Windows laptop, edge-2) it dials the cable address.
        if [ "${FLIGHTRISK_GROUND_LOCAL:-}" = "1" ]; then
            export FLIGHTRISK_EDGE_WS="ws://127.0.0.1:9000"
        else
            export FLIGHTRISK_EDGE_WS="ws://10.42.0.1:9000"
        fi
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
