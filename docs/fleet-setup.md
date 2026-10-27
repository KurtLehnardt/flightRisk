# Fleet setup: two Tello EDUs, two laptops

This guide gets two Tello EDU drones flying under one FlightRisk ground station. There are two network paths. Both use the same protocol and the same coordinator code. A single variable, `FLIGHTRISK_FLEET_NET`, selects the path, and `scripts/fleet_env.sh` (Mac) or `scripts/fleet_env.ps1` (Windows) turns it into the host and URL settings the launchers need.

Layout in both paths: the ground station and edge-1 run on the Mac, and edge-2 runs on the Windows laptop. Drone 1 is controlled from the Mac and drone 2 from the Windows laptop.

| | Path 1: station mode | Path 2: AP mode |
|---|---|---|
| Drones join | one shared 2.4 GHz router or phone hotspot | their own Wi-Fi, one drone per laptop |
| Laptops are linked by | the same router or hotspot | a wired Ethernet cable on 10.42.0.0/24 |
| Extra hardware | router or hotspot | Ethernet adapters and one cable |
| `FLIGHTRISK_FLEET_NET` | `station` | `ap` |
| Use it when | the hardware check passes | station mode fails (for example the `ap` command is rejected) |

Station mode is the default if the hardware check passes. AP mode is the fully designed fallback.

## Placement and heading

Do this before every flight, in both paths. The coordinator assumes this layout, so a different one sends a drone toward the audience or a wall.

1. Mark a 4 m × 4 m area and a takeoff line along its near edge.
2. Put drone-1 at the near-left corner of the area.
3. Put drone-2 250 cm to its right on the takeoff line.
4. Point both noses into the area along the 4 m depth.
5. Keep the audience behind the takeoff line. The presenter stands at least 1.5 m beyond the far edge, outside the geofence and never in a flight path.

The launchers set the edge geofence to 450 cm. Override it with `FLIGHTRISK_GEOFENCE_CM` only if the floor is larger.

## Video port verdict

Each Tello streams video to UDP port 11111 on whichever host sent `streamon`, and djitellopy binds one video capture per port, so two drones streaming to one host would collide. The SDK command `port <state> <video>` could move the video port, but whether Tello EDU firmware accepts it is unverified, because the SDK 2.0 and 3.0 documents could not be text-extracted when this was designed. The design therefore does not depend on `port`: run one edge process per host. Drone 1 gets `streamon` from the Mac and drone 2 gets it from the Windows laptop, so each drone's video goes to a different IP. This holds in both paths. The hardware check probes `port 8890 11112` on drone 2 and reports `ok` or `unknown command`. If it reports `ok`, a one-host two-edge layout becomes an option, still with one edge process per drone, but nothing here requires it.

## Common setup (both paths)

Pick one shared secret and set it on both laptops. The ground station binds a non-loopback address, so it refuses to start without a token.

macOS:

```
export FLIGHTRISK_EDGE_TOKEN="choose-a-long-random-string"
```

Windows (PowerShell):

```
$env:FLIGHTRISK_EDGE_TOKEN = "choose-a-long-random-string"
```

Use the same value on both machines. Run the hardware check before anything else, from the repository root on the Mac:

```
venv/bin/python scripts/tello_hw_check.py --drones 192.168.10.1 --json
```

## Wi-Fi reset with the power button

If a drone is stuck in station mode, will not answer, or you lost its network, hold its power button for about 5 seconds (5 s). The drone restarts and returns to factory AP mode, where it broadcasts its own `TELLO-XXXXXX` network and answers at 192.168.10.1. Do this for any drone before starting the AP-mode path, and as the way back from station mode.

## Path 1: station mode

Hardware: one 2.4 GHz router or phone hotspot. The Tello radio is 2.4 GHz only. If you use a hotspot, it must allow client-to-client traffic (no client isolation), and both laptops and both drones must join it.

### 1. Put each drone on the router

Do this once per drone, one drone at a time, with the other drone powered off. Only an EDU supports the `ap` command, and some EDU units answer `unknown command: ap` until the firmware is updated. If yours does, use Path 2.

1. Power on drone 1 and join your laptop to its `TELLO-XXXXXX` network.
2. Send the `ap` command through the hardware check. It is never sent automatically, it needs the explicit flag, and it reconfigures the drone:

```
venv/bin/python scripts/tello_hw_check.py --drones 192.168.10.1 --station-mode "YourSSID" "YourPassword" --i-understand-this-reconfigures-the-drone
```

   The Wi-Fi password lands in your shell history if you type it on the command line. Keep it out: start the line with a space after `export HISTCONTROL=ignorespace` in bash (zsh: `setopt HIST_IGNORE_SPACE`), or read it into a variable without echo and pass the variable:

```
read -rs WIFI_PASS
venv/bin/python scripts/tello_hw_check.py --drones 192.168.10.1 --station-mode "YourSSID" "$WIFI_PASS" --i-understand-this-reconfigures-the-drone
unset WIFI_PASS
```

3. The drone restarts and joins the router. Rejoin your laptop to the router, then repeat for drone 2.

### 2. Discover the drones

Join both laptops to the router. Find the router subnet (for example 192.168.1.0/24) and scan it. The check finds drones that answer `command` on UDP port 8889:

```
venv/bin/python scripts/tello_hw_check.py --discover 192.168.1.0/24
```

Note the two drone IPs, for example 192.168.1.50 and 192.168.1.51. Then verify command, state and video round-trips on each, and run the video probe:

```
venv/bin/python scripts/tello_hw_check.py --drones 192.168.1.50,192.168.1.51
venv/bin/python scripts/tello_hw_check.py --drones 192.168.1.50 --video-check
```

The state stream arrives on UDP port 8890 and video on UDP port 11111. The video check counts frames for 5 seconds. If everything passes, continue. If the `ap` step or the round-trips fail, switch to Path 2.

### 3. Find the Mac's LAN IP

On the Mac:

```
ipconfig getifaddr en0
```

Use `en1` if `en0` prints nothing.

### 4. Export the settings

On the Mac (drone 1 is 192.168.1.50, Mac LAN IP is 192.168.1.20 in this example):

```
export FLIGHTRISK_FLEET_NET=station
export FLIGHTRISK_GROUND_IP=192.168.1.20
export FLIGHTRISK_TELLO_HOST=192.168.1.50
source scripts/fleet_env.sh
```

On Windows (drone 2 is 192.168.1.51):

```
$env:FLIGHTRISK_FLEET_NET = "station"
$env:FLIGHTRISK_GROUND_IP = "192.168.1.20"
$env:FLIGHTRISK_TELLO_HOST = "192.168.1.51"
. .\scripts\fleet_env.ps1
```

The script sets `FLIGHTRISK_EDGE_HOST` to the Mac LAN IP (the ground station binds it) and `FLIGHTRISK_EDGE_WS` to `ws://<mac lan ip>:9000` (the edge connects to it). Allow the macOS firewall prompt for Python on first run, or see the firewall note in Path 2.

Continue at "Launch" below.

## Path 2: AP mode with a wired laptop link

Each laptop joins one Tello's own Wi-Fi, so that Wi-Fi radio is busy and cannot reach the other laptop. The laptops talk over a cable instead.

### Hardware list

- One USB-C Gigabit Ethernet adapter for the Mac.
- One USB-C or USB-A Gigabit Ethernet adapter for the Windows laptop, only if it has no RJ45 port.
- One Cat5e or Cat6 Ethernet patch cable. A crossover cable is not needed because gigabit adapters auto-negotiate the pair swap (auto-MDIX).
- Two Tello EDUs in factory AP mode (hold the power button for about 5 seconds if unsure, see the reset section above).

### Addressing plan

| Item | Value |
|---|---|
| Subnet | 10.42.0.0/24, static, no gateway, no DNS |
| Mac (ground station, edge-1) | 10.42.0.1 |
| Windows (edge-2) | 10.42.0.2 |
| Ground WebSocket | `ws://10.42.0.1:9000` |
| Tello address on both laptops | 192.168.10.1 (each laptop sees only its own drone) |

Leave the gateway blank. The laptops keep their Wi-Fi for the drones, and a gateway on the cable would pull the default route off the drone network.

### 1. Connect the cable

Plug the adapters and the cable in so the two laptops are connected directly.

### 2. Configure the Mac

List the hardware ports and note the adapter's service name (typically "USB 10/100/1000 LAN") and its device (for example `en4`):

```
networksetup -listallhardwareports
```

Set the static address, using your service name:

```
sudo networksetup -setmanual "USB 10/100/1000 LAN" 10.42.0.1 255.255.255.0
```

If you do not know the service name, use this non-persistent alternative with the device from the list above:

```
sudo ifconfig en4 inet 10.42.0.1 netmask 255.255.255.0
```

If the macOS application firewall is on, accept the "python wants to accept incoming connections" prompt, or add Python explicitly from the repository root:

```
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$PWD/venv/bin/python"
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp "$PWD/venv/bin/python"
```

### 3. Configure Windows

Open PowerShell as administrator. Find the adapter's alias (typically "Ethernet"):

```
Get-NetAdapter
```

Then set the static address with no gateway, mark the link as a Private network, and add the firewall rules. Replace "Ethernet" with your alias if it differs:

```
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.42.0.2 -PrefixLength 24
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
New-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow
New-NetFirewallRule -DisplayName "FlightRisk ground 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow
```

The ICMP rule lets the Mac ping the Windows laptop during the link check. The TCP 9000 rule is only used if you move the ground station to the Windows laptop. In the default layout the Windows laptop only makes an outbound connection to 10.42.0.1:9000, which Windows Defender Firewall allows by default.

### 4. Check the cable link

On the Mac, start the server side (it pings 10.42.0.2 and listens on 10.42.0.1:9000):

```
venv/bin/python scripts/check_fleet_link.py --role server
```

On Windows, run the client side (it pings 10.42.0.1, opens `ws://10.42.0.1:9000`, sends 8 ping frames and prints the median round-trip time):

```
venv\Scripts\python.exe scripts\check_fleet_link.py --role client --peer 10.42.0.1
```

Both sides print `PASS` or `FAIL` for the ping and for the WebSocket. Stop the server with Ctrl-C when done. If you only have the hardware check script, the same test is available as `scripts/tello_hw_check.py --fleet-net ap --link-role server` on the Mac and `--fleet-net ap --link-role client --peer 10.42.0.1` on Windows.

If the ping fails, recheck the addresses with `ifconfig` (Mac) or `Get-NetIPAddress` (Windows) and confirm the firewall rules. If only the WebSocket fails, the server side is not running yet.

### 5. Join the drones and check them

Join the Mac's Wi-Fi to drone 1's `TELLO-XXXXXX` network and the Windows laptop's Wi-Fi to drone 2's. Then verify plain AP mode on each laptop:

```
venv/bin/python scripts/tello_hw_check.py --ap-mode-check
```

On Windows use `venv\Scripts\python.exe scripts\tello_hw_check.py --ap-mode-check`.

### 6. Export the settings

On the Mac:

```
export FLIGHTRISK_FLEET_NET=ap
source scripts/fleet_env.sh
```

On Windows:

```
$env:FLIGHTRISK_FLEET_NET = "ap"
. .\scripts\fleet_env.ps1
```

In AP mode the script fixes `FLIGHTRISK_TELLO_HOST` to 192.168.10.1 on both hosts, `FLIGHTRISK_EDGE_HOST` to 10.42.0.1 and `FLIGHTRISK_EDGE_WS` to `ws://10.42.0.1:9000`.

## Launch

Run the launchers after exporting the settings and `FLIGHTRISK_EDGE_TOKEN`. Start the ground station first.

On the Mac, in one terminal for the ground station and another for edge-1:

```
scripts/demo_ground.sh
DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
```

On Windows, for edge-2:

```
$env:DRONE_ID = "2"; $env:ALT_CM = "160"; .\scripts\demo_edge.ps1
```

Lay out the drones as in "Placement and heading" above first. The dashboard should show both drones with their clock offsets and round-trip times. Check each battery is at least 30 percent and the floor area is clear before takeoff.

## Quick reference

| Setting | Station | AP |
|---|---|---|
| `FLIGHTRISK_FLEET_NET` | `station` | `ap` |
| `FLIGHTRISK_TELLO_HOST` | discovered drone IP, set by you | 192.168.10.1 |
| `FLIGHTRISK_EDGE_HOST` | Mac LAN IP | 10.42.0.1 |
| `FLIGHTRISK_EDGE_WS` | `ws://<mac lan ip>:9000` | `ws://10.42.0.1:9000` |
| `FLIGHTRISK_EDGE_TOKEN` | required, same on both | required, same on both |
| Tello ports | UDP 8889 commands, 8890 state, 11111 video | same |
