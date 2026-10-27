# Dot-source this file (do not run it in a child process) to set the fleet network settings.
#
#   $env:FLIGHTRISK_FLEET_NET = "station"   # or: "ap"
#   . .\scripts\fleet_env.ps1
#
# station mode needs FLIGHTRISK_GROUND_IP (the Mac's LAN IP) and FLIGHTRISK_TELLO_HOST
# (the IP of the Tello that this host controls, from the discovery step).
# ap mode needs nothing else: the addresses are fixed by the wired-link plan.
# Both modes need FLIGHTRISK_EDGE_TOKEN to be set, because the ground station binds
# a non-loopback address.
#
# ap mode binds the ground station to "127.0.0.1,10.42.0.1" (FLIGHTRISK_EDGE_HOST). Set
#   $env:FLIGHTRISK_GROUND_LOCAL = "1"
# on a machine that runs the ground station itself so its edge connects to
# ws://127.0.0.1:9000. Leave it unset on the Windows laptop, whose edge-2 connects to
# ws://10.42.0.1:9000. Station mode ignores FLIGHTRISK_GROUND_LOCAL.

$fleetNet = $env:FLIGHTRISK_FLEET_NET

if ($fleetNet -eq "station") {
    if (-not $env:FLIGHTRISK_GROUND_IP) {
        Write-Error "fleet_env.ps1: station mode needs FLIGHTRISK_GROUND_IP (the Mac LAN IP)"
        return
    }
    if (-not $env:FLIGHTRISK_TELLO_HOST) {
        Write-Error "fleet_env.ps1: station mode needs FLIGHTRISK_TELLO_HOST (the discovered Tello IP for this host)"
        return
    }
    $env:FLIGHTRISK_EDGE_HOST = $env:FLIGHTRISK_GROUND_IP
    $env:FLIGHTRISK_EDGE_WS = "ws://$($env:FLIGHTRISK_GROUND_IP):9000"
}
elseif ($fleetNet -eq "ap") {
    $env:FLIGHTRISK_GROUND_IP = "10.42.0.1"
    $env:FLIGHTRISK_TELLO_HOST = "192.168.10.1"
    # Ground bind list: loopback first, then the wired-link address. Never a wildcard.
    $env:FLIGHTRISK_EDGE_HOST = "127.0.0.1,10.42.0.1"
    # Where an edge connects. FLIGHTRISK_GROUND_LOCAL=1 means the ground station runs on
    # this machine, so the edge uses loopback and survives the cable being pulled.
    # Otherwise (the Windows laptop, edge-2) it dials the cable address.
    if ($env:FLIGHTRISK_GROUND_LOCAL -eq "1") {
        $env:FLIGHTRISK_EDGE_WS = "ws://127.0.0.1:9000"
    }
    else {
        $env:FLIGHTRISK_EDGE_WS = "ws://10.42.0.1:9000"
    }
}
else {
    Write-Error "fleet_env.ps1: set FLIGHTRISK_FLEET_NET to station or ap (got '$fleetNet')"
    return
}

if (-not $env:FLIGHTRISK_EDGE_TOKEN) {
    Write-Warning "fleet_env.ps1: FLIGHTRISK_EDGE_TOKEN is not set; the ground station refuses to start without it."
}

Write-Host "fleet_env.ps1: FLIGHTRISK_FLEET_NET=$fleetNet edge-host=$($env:FLIGHTRISK_EDGE_HOST) tello-host=$($env:FLIGHTRISK_TELLO_HOST) ws=$($env:FLIGHTRISK_EDGE_WS)"
