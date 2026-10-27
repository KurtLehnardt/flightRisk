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
    $env:FLIGHTRISK_EDGE_HOST = "10.42.0.1"
    $env:FLIGHTRISK_EDGE_WS = "ws://10.42.0.1:9000"
}
else {
    Write-Error "fleet_env.ps1: set FLIGHTRISK_FLEET_NET to station or ap (got '$fleetNet')"
    return
}

if (-not $env:FLIGHTRISK_EDGE_TOKEN) {
    Write-Warning "fleet_env.ps1: FLIGHTRISK_EDGE_TOKEN is not set; the ground station refuses to start without it."
}

Write-Host "fleet_env.ps1: FLIGHTRISK_FLEET_NET=$fleetNet edge-host=$($env:FLIGHTRISK_EDGE_HOST) tello-host=$($env:FLIGHTRISK_TELLO_HOST) ws=$($env:FLIGHTRISK_EDGE_WS)"
