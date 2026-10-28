# Start two FlightRisk edges on one Windows laptop, one per Wi-Fi adapter and Tello.
#
#   $env:FLIGHTRISK_EDGE_TOKEN = "the same secret as on the ground station"
#   .\scripts\demo_edge_pair.ps1 -LocalIp1 192.168.10.2 -LocalIp2 192.168.10.50 -IfIndex1 12 -IfIndex2 23
#
# Each edge runs in its own console window with QuickEdit turned off, so a stray click in the window
# cannot pause the edge. Edge 1 flies drone-1 at altitude slot 50 cm through the adapter with
# LocalIp1; edge 2 flies drone-2 at 80 cm through the adapter with LocalIp2. Both dial the ground
# station at ws://10.42.0.1:9000 (the Mac, over the Thunderbolt cable). Get the IPs and interface
# indexes with:
#
#   Get-NetIPAddress -AddressFamily IPv4 | Format-Table InterfaceIndex, InterfaceAlias, IPAddress
#
# The edge token is read from FLIGHTRISK_EDGE_TOKEN, passed to the edge windows through the
# environment, and never printed. -DryRun prints both edge command lines without starting anything.
# Each window also writes its output to logs\edge-<drone id>-<time>.log (-LogDir changes the folder,
# -NoLog turns it off), which scripts\flight_log_summary.ps1 can summarise.
# See docs/two-tellos-one-pc.md.

[CmdletBinding(DefaultParameterSetName = "Pair")]
param(
    [Parameter(ParameterSetName = "Pair", Mandatory = $true)] [string]$LocalIp1,
    [Parameter(ParameterSetName = "Pair", Mandatory = $true)] [string]$LocalIp2,
    [Parameter(ParameterSetName = "Pair")] [int]$IfIndex1 = 0,
    [Parameter(ParameterSetName = "Pair")] [int]$IfIndex2 = 0,
    [Parameter(ParameterSetName = "Pair")] [string]$DroneId1 = "drone-1",
    [Parameter(ParameterSetName = "Pair")] [string]$DroneId2 = "drone-2",
    [Parameter(ParameterSetName = "Pair")] [int]$Slot1 = 50,
    [Parameter(ParameterSetName = "Pair")] [int]$Slot2 = 80,
    [Parameter(ParameterSetName = "Pair")] [switch]$DryRun,
    [Parameter(ParameterSetName = "Pair")] [string]$LogDir = "logs",
    [Parameter(ParameterSetName = "Pair")] [switch]$NoLog,

    # Internal: run one edge in this window (used by the pair mode above).
    [Parameter(ParameterSetName = "Child", Mandatory = $true)] [switch]$Child,
    [Parameter(ParameterSetName = "Child", Mandatory = $true)] [string]$LocalIp,
    [Parameter(ParameterSetName = "Child")] [int]$IfIndex = 0,
    [Parameter(ParameterSetName = "Child", Mandatory = $true)] [string]$DroneId,
    [Parameter(ParameterSetName = "Child", Mandatory = $true)] [int]$Slot,
    [Parameter(ParameterSetName = "Child")] [string]$LogFile = "",

    [string]$TelloHost = "192.168.10.1",
    [string]$GroundUrl = "ws://10.42.0.1:9000",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not $Python) {
    $Python = if ($env:PYTHON) { $env:PYTHON } else { "venv\Scripts\python.exe" }
}

function Get-EdgeArgs([string]$ip, [int]$ifIndex, [string]$droneId, [int]$slot) {
    $edgeArgs = @(
        "-m", "flightrisk.edge_device",
        "--source", "tello",
        "--tello-host", $TelloHost,
        "--tello-local-ip", $ip
    )
    if ($ifIndex -gt 0) { $edgeArgs += @("--tello-ifindex", "$ifIndex") }
    $edgeArgs += @("--drone-id", $droneId, "--alt-slot", "$slot", "--ws-url", $GroundUrl)
    return , $edgeArgs
}

function Disable-QuickEdit {
    # QuickEdit pauses a console program while text is selected, so one click in the window would
    # freeze the edge (and its keepalive). Turn it off for this console window only.
    if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") { return }
    try {
        if (-not ("FlightRiskConsoleMode" -as [type])) {
            Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class FlightRiskConsoleMode {
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetConsoleMode(IntPtr h, out uint mode);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetConsoleMode(IntPtr h, uint mode);
    public static bool DisableQuickEdit() {
        IntPtr input = GetStdHandle(-10);
        uint mode;
        if (!GetConsoleMode(input, out mode)) return false;
        mode &= ~0x0040u;  // ENABLE_QUICK_EDIT_MODE
        mode |= 0x0080u;   // ENABLE_EXTENDED_FLAGS, needed for the change to take effect
        return SetConsoleMode(input, mode);
    }
}
"@
        }
        if (-not [FlightRiskConsoleMode]::DisableQuickEdit()) {
            Write-Warning "Could not turn off QuickEdit; do not click inside this window while the edge runs."
        }
    }
    catch {
        Write-Warning "Could not turn off QuickEdit ($($_.Exception.Message)); do not click inside this window."
    }
}

if ($PSCmdlet.ParameterSetName -eq "Child") {
    $Host.UI.RawUI.WindowTitle = "FlightRisk edge $DroneId via $LocalIp"
    Disable-QuickEdit
    # Same environment as scripts\demo_edge.ps1: a 450 cm geofence unless set, appearance-only matching.
    # This window is its own process, so nothing leaks back into the operator's shell.
    if (-not $env:FLIGHTRISK_GEOFENCE_CM) { $env:FLIGHTRISK_GEOFENCE_CM = "450" }
    $env:FLIGHTRISK_APPEARANCE_ONLY = "1"
    $edgeArgs = Get-EdgeArgs $LocalIp $IfIndex $DroneId $Slot
    Write-Host "Starting $DroneId through $LocalIp (slot $Slot cm, ground $GroundUrl)"
    # From here on an error line from the edge must never stop this script (and with it the edge's
    # output pipe): Windows PowerShell 5.1 turns redirected stderr lines into errors.
    $ErrorActionPreference = "Continue"
    if ($LogFile) {
        # Unbuffered, so the window shows each line as it happens even though output goes through a pipe.
        $env:PYTHONUNBUFFERED = "1"
        Write-Host "Logging to $LogFile"
        & $Python @edgeArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $LogFile
    }
    else {
        & $Python @edgeArgs
    }
    Write-Host "Edge $DroneId exited with code $LASTEXITCODE. Close this window when done."
    exit $LASTEXITCODE
}

# --- Pair mode: check everything, then start one window per edge ---

function Test-Ip([string]$name, [string]$value) {
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($value, [ref]$parsed) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $value -eq "0.0.0.0") {
        Write-Error "demo_edge_pair.ps1: $name must be the IPv4 address of one Wi-Fi adapter (got '$value')"
        exit 1
    }
}

Test-Ip "-LocalIp1" $LocalIp1
Test-Ip "-LocalIp2" $LocalIp2
if ($LocalIp1 -eq $LocalIp2) {
    Write-Error "demo_edge_pair.ps1: -LocalIp1 and -LocalIp2 are both $LocalIp1. Each Wi-Fi adapter needs its own IP (docs/two-tellos-one-pc.md)."
    exit 1
}
if ($DroneId1 -eq $DroneId2) {
    Write-Error "demo_edge_pair.ps1: the two edges need different drone ids (got '$DroneId1' twice)"
    exit 1
}
foreach ($slot in @($Slot1, $Slot2)) {
    if ($slot -lt 10) {
        Write-Error "demo_edge_pair.ps1: altitude slots are in cm and at least 10 (got $slot)"
        exit 1
    }
}
if ($IfIndex1 -gt 0 -and $IfIndex1 -eq $IfIndex2) {
    Write-Error "demo_edge_pair.ps1: -IfIndex1 and -IfIndex2 are both $IfIndex1; each adapter has its own index"
    exit 1
}

# Where Get-NetIPAddress exists (Windows), check that each IP is on exactly one adapter and, when an
# interface index is given, that it is that adapter's index. The edge checks the IPs again itself.
if (Get-Command Get-NetIPAddress -ErrorAction SilentlyContinue) {
    foreach ($pair in @(@($LocalIp1, $IfIndex1, "-LocalIp1"), @($LocalIp2, $IfIndex2, "-LocalIp2"))) {
        $found = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $pair[0] -ErrorAction SilentlyContinue)
        if ($found.Count -eq 0) {
            Write-Error "demo_edge_pair.ps1: $($pair[2]) $($pair[0]) is not assigned to any adapter. Is the adapter plugged in and joined to the drone's Wi-Fi?"
            exit 1
        }
        if ($found.Count -gt 1) {
            Write-Error "demo_edge_pair.ps1: $($pair[0]) is on more than one adapter; give each adapter its own IP"
            exit 1
        }
        if ($pair[1] -gt 0 -and $found[0].InterfaceIndex -ne $pair[1]) {
            Write-Error "demo_edge_pair.ps1: $($pair[0]) is on interface index $($found[0].InterfaceIndex), not $($pair[1])"
            exit 1
        }
    }
}

if (-not $env:FLIGHTRISK_EDGE_TOKEN) {
    Write-Error @"
demo_edge_pair.ps1: FLIGHTRISK_EDGE_TOKEN is not set, so the edges cannot authenticate to the ground station.
Set it in this window first (the same secret as on the ground station):

    `$env:FLIGHTRISK_EDGE_TOKEN = "..."
"@
    exit 1
}

$edges = @(
    @{ Ip = $LocalIp1; IfIndex = $IfIndex1; DroneId = $DroneId1; Slot = $Slot1 },
    @{ Ip = $LocalIp2; IfIndex = $IfIndex2; DroneId = $DroneId2; Slot = $Slot2 }
)

if ($DryRun) {
    foreach ($e in $edges) {
        $edgeArgs = Get-EdgeArgs $e.Ip $e.IfIndex $e.DroneId $e.Slot
        Write-Host "$Python $($edgeArgs -join ' ')"
    }
    exit 0
}

# The windows inherit this process's environment, which carries the token; it is never on a command line.
$shell = (Get-Process -Id $PID).Path
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $NoLog) {
    $logRoot = if ([System.IO.Path]::IsPathRooted($LogDir)) { $LogDir } else { Join-Path $root $LogDir }
    New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
}
foreach ($e in $edges) {
    $childArgs = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", "`"$PSCommandPath`"",
        "-Child", "-LocalIp", $e.Ip, "-DroneId", $e.DroneId, "-Slot", "$($e.Slot)",
        "-TelloHost", $TelloHost, "-GroundUrl", $GroundUrl, "-Python", "`"$Python`""
    )
    if ($e.IfIndex -gt 0) { $childArgs += @("-IfIndex", "$($e.IfIndex)") }
    if (-not $NoLog) {
        $childArgs += @("-LogFile", "`"$(Join-Path $logRoot "edge-$($e.DroneId)-$stamp.log")`"")
    }
    Start-Process -FilePath $shell -ArgumentList $childArgs -WorkingDirectory $root | Out-Null
    Write-Host "Started $($e.DroneId) through $($e.Ip) at slot $($e.Slot) cm in a new window."
}
