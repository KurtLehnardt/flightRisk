# Start one FlightRisk edge process that flies one Tello (run on the Windows laptop for drone 2).
#
#   $env:FLIGHTRISK_FLEET_NET = "ap"            # or "station"; see docs/fleet-setup.md
#   $env:FLIGHTRISK_EDGE_TOKEN = "choose-a-long-random-string"
#   $env:DRONE_ID = "2"; $env:ALT_CM = "160"; .\scripts\demo_edge.ps1
#
# DRONE_ID is 1 or 2 (or a full id such as drone-2) and becomes the fleet id drone-1 or
# drone-2, which the ground station's --fleet list must contain. Default: 2.
# ALT_CM is this drone's altitude slot in cm (at least 10). Default: 100 for odd ids and
# 160 for even ids, the same alternation the coordinator uses.
# The Tello address and ground URL come from scripts\fleet_env.ps1. In ap mode, set
# $env:FLIGHTRISK_GROUND_LOCAL = "1" only on a machine that runs the ground station itself, so
# its edge connects to ws://127.0.0.1:9000; otherwise the edge dials ws://10.42.0.1:9000.
# Set DEMO_DRY_RUN=1 to print the command instead of running it.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

. (Join-Path $PSScriptRoot "fleet_env.ps1")

if ($env:FLIGHTRISK_GROUND_LOCAL -eq "1") {
    Write-Warning "FLIGHTRISK_GROUND_LOCAL=1 means the ground station runs on THIS machine; unset it on the edge-only laptop."
}

if (-not $env:FLIGHTRISK_EDGE_TOKEN) {
    Write-Error @"
demo_edge.ps1: FLIGHTRISK_EDGE_TOKEN is not set, so the edge cannot authenticate to the ground station.
Use the same secret as on the ground station:

    `$env:FLIGHTRISK_EDGE_TOKEN = "choose-a-long-random-string"
"@
    exit 1
}

$rawId = if ($env:DRONE_ID) { $env:DRONE_ID } else { "2" }
if ($rawId -match '^(?:drone-)?(\d+)$') {
    $number = [int]$Matches[1]
}
else {
    Write-Error "demo_edge.ps1: DRONE_ID must be a number such as 1 or 2 (got '$rawId')"
    exit 1
}
$droneId = "drone-$number"

if ($env:ALT_CM) {
    if ($env:ALT_CM -notmatch '^\d+$') {
        Write-Error "demo_edge.ps1: ALT_CM must be a whole number of centimetres (got '$($env:ALT_CM)')"
        exit 1
    }
    $altCm = [int]$env:ALT_CM
}
else {
    $altCm = if ($number % 2 -eq 1) { 100 } else { 160 }
}
if ($altCm -lt 10) {
    Write-Error "demo_edge.ps1: ALT_CM must be at least 10; smaller values are read as metres"
    exit 1
}

# The live floor is 4 m x 4 m, so the geofence is 300 cm unless the environment overrides it,
# and the edge matches on appearance only: it builds no face recognizer and sends no face embeddings.
# These variables are set only for this script's run and restored in the finally block, so a later
# run in the same PowerShell session does not inherit them.
$savedGeofence = $env:FLIGHTRISK_GEOFENCE_CM
$savedAppearanceOnly = $env:FLIGHTRISK_APPEARANCE_ONLY
$exitCode = 0
try {
    if (-not $env:FLIGHTRISK_GEOFENCE_CM) { $env:FLIGHTRISK_GEOFENCE_CM = "300" }
    $env:FLIGHTRISK_APPEARANCE_ONLY = "1"

    $python = if ($env:PYTHON) { $env:PYTHON } else { "venv\Scripts\python.exe" }

    $edgeArgs = @(
        "-m", "flightrisk.edge_device",
        "--source", "tello",
        "--tello-host", $env:FLIGHTRISK_TELLO_HOST,
        "--drone-id", $droneId,
        "--alt-slot", "$altCm",
        "--ws-url", $env:FLIGHTRISK_EDGE_WS
    )

    if ($env:DEMO_DRY_RUN) {
        Write-Host "$python $($edgeArgs -join ' ')"
    }
    else {
        & $python @edgeArgs
        $exitCode = $LASTEXITCODE
    }
}
finally {
    $env:FLIGHTRISK_GEOFENCE_CM = $savedGeofence
    $env:FLIGHTRISK_APPEARANCE_ONLY = $savedAppearanceOnly
}
exit $exitCode
