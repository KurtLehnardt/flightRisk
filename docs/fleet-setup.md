# Fleet setup: two Tello EDUs, two laptops

This guide gets two Tello EDU drones flying under one FlightRisk ground station. There are three network paths: station mode, AP mode with an Ethernet link, and AP mode with a Thunderbolt link (Path 3, which has the same addresses as Path 2). All use the same protocol and the same coordinator code. A single variable, `FLIGHTRISK_FLEET_NET`, selects the path, and `scripts/fleet_env.sh` (Mac) or `scripts/fleet_env.ps1` (Windows) turns it into the host and URL settings the launchers need.

Layout in both paths: the ground station and edge-1 run on the Mac, and edge-2 runs on the Windows laptop. Drone 1 is controlled from the Mac and drone 2 from the Windows laptop.

| | Path 1: station mode | Path 2: AP mode |
|---|---|---|
| Drones join | one shared 2.4 GHz router or phone hotspot | their own Wi-Fi, one drone per laptop |
| Laptops are linked by | the same router or hotspot | a wired Ethernet cable on 10.42.0.0/24 |
| Extra hardware | router or hotspot | Ethernet adapters and one cable |
| `FLIGHTRISK_FLEET_NET` | `station` | `ap` |
| Use it when | the hardware check passes | station mode fails (for example the `ap` command is rejected) |

Station mode is the default if the hardware check passes. AP mode is the fully designed fallback. Path 3 swaps the Ethernet cable of Path 2 for a Thunderbolt or USB4 cable and is UNVERIFIED on our hardware.

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
| Ground WebSocket | binds `127.0.0.1` and `10.42.0.1`; edge-1 on the Mac uses `ws://127.0.0.1:9000`, edge-2 on Windows uses `ws://10.42.0.1:9000` |
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

In AP mode the script fixes `FLIGHTRISK_TELLO_HOST` to 192.168.10.1 on both hosts and `FLIGHTRISK_EDGE_HOST` to the bind list `127.0.0.1,10.42.0.1` (loopback first, then the cable address; never a wildcard). The ground station needs `FLIGHTRISK_EDGE_TOKEN` because one of those addresses is not loopback. If the cable address is missing at start, the ground station listens on loopback only and retries.

`FLIGHTRISK_EDGE_WS` depends on where the edge runs, and you say so explicitly with `FLIGHTRISK_GROUND_LOCAL`. On the Mac, which runs the ground station and edge-1, run `export FLIGHTRISK_GROUND_LOCAL=1` before sourcing the script and `FLIGHTRISK_EDGE_WS` becomes `ws://127.0.0.1:9000`. The variable must be exported, not just set: `scripts/demo_edge.sh` runs as a child process and sources `fleet_env.sh` again, so an unexported variable is lost and the edge would dial the cable address even though the earlier output said loopback. Only the exact value `1` counts; any other non-empty value other than `0` prints a warning and uses the cable address. On the Windows edge-only laptop, never set it. On Windows leave it unset and `FLIGHTRISK_EDGE_WS` is `ws://10.42.0.1:9000`. Nothing guesses: without the variable every edge dials `ws://10.42.0.1:9000`, as before. Station mode ignores the variable.

With this setup, pulling the cable is expected to leave edge-1 on loopback, so it should keep flying or holding, and edge-2 should lose its link, hit its watchdog and land. UNVERIFIED on hardware; step 8 measures it. If 10.42.0.1 goes away (step 6 shows whether it does), the ground station logs `ground_bind_address_lost`, and `ground_bind_address_restored` when it returns.

Precondition: port 9000 on the Mac's 127.0.0.1 must be free, because a loopback bind failure stops the ground station (loudly, before flight). Homebrew's php-fpm is one default that uses it. Check with `lsof -nP -iTCP:9000 -sTCP:LISTEN`; no output means it is free.

## Path 3: Thunderbolt link

Path 3 is Path 2 with a Thunderbolt or USB4 cable in place of the two Ethernet adapters and the Cat6 cable. Each laptop's Wi-Fi still joins its own Tello. The cable carries the same subnet and the same addresses as Path 2, so `FLIGHTRISK_FLEET_NET=ap` and every script work unchanged.

Status: this path is UNVERIFIED on our hardware. Mac to Windows IP over Thunderbolt is documented only in one blog report, and nobody has tested the pair of laptops used here. Run the validation checklist below before relying on it. If a step fails, use the fallback ladder.

### What is known and what is not

- VERIFIED (Apple support article): two Macs connected directly by a Thunderbolt cable can network with "Thunderbolt Bridge". A plain USB-C charge cable does not work. Do not use a dock or a hub.
- VERIFIED (Microsoft documentation): Windows 11 has built-in Ethernet over USB4, and it works with a Thunderbolt 3 peer. It can be disabled by Group Policy. UNVERIFIED: which Windows 11 build is needed.
- VERIFIED as one blog report only: a Mac (M2, Thunderbolt 4) and a Windows 11 PC (Thunderbolt 4) linked with no adapters and ran at over 13 Gbps. FlightRisk needs well under 1 percent of that.
- VERIFIED as one forum thread only: Windows 10 with Intel Thunderbolt 3 networking was unreliable. A Mac booted into Windows 10 with Boot Camp got no bridge at all. Treat Windows 10 as unsupported.
- UNVERIFIED: whether a Windows laptop with an older Intel Thunderbolt 3 controller links to a Mac. The only way to find out is to plug in and look for a new network adapter.
- UNVERIFIED: that the macOS network service is named exactly "Thunderbolt Bridge" and that its interface is `bridge0`. Check both with the commands below.
- UNVERIFIED: whether macOS keeps the address 10.42.0.1 while the cable is unplugged. Step 6 and step 8 measure this.
- UNVERIFIED: whether the Windows laptop has Thunderbolt or USB4 at all. Step 1 checks this.

### Address plan

| Item | Value |
|---|---|
| Subnet | 10.42.0.0/24, static, no gateway, no DNS |
| Mac (ground station, edge-1) | 10.42.0.1 |
| Windows (edge-2) | 10.42.0.2 |
| Ground WebSocket | binds `127.0.0.1` and `10.42.0.1`; edge-1 on the Mac uses `ws://127.0.0.1:9000`, edge-2 on Windows uses `ws://10.42.0.1:9000` |
| Tello address on both laptops | 192.168.10.1 over Wi-Fi |

Do not use the self-assigned 169.254.x.x addresses for the demo. They change whenever the Windows adapter is recreated, which happens on every replug. They are still useful to prove the physical link: if a static address does not work, ping the peer's 169.254 address shown by `ipconfig` or `ifconfig`.

Never turn on macOS Internet Sharing or Windows Internet Connection Sharing. Either one would bridge one Tello's 192.168.10.0/24 network onto the cable.

### Mac steps

1. List the network services and find the Thunderbolt one:

   ```
   networksetup -listallhardwareports
   ```

2. Set the static address with no router argument. Replace the service name if yours differs:

   ```
   sudo networksetup -setmanual "Thunderbolt Bridge" 10.42.0.1 255.255.255.0
   ```

3. Check the address and the routes:

   ```
   ifconfig bridge0
   route -n get 10.42.0.2
   route -n get 192.168.10.1
   netstat -rn -f inet | grep -E "default|10.42|192.168.10"
   ```

   `route -n get 10.42.0.2` must show the Thunderbolt interface. `route -n get 192.168.10.1` must show Wi-Fi once the Mac has joined its drone. No default route may point at the Thunderbolt interface.

4. If the macOS application firewall is on, allow Python as in Path 2, step 2.
5. Keep the Mac awake for the whole session, and never close the lid. Record before you change: run `pmset -g custom` and write down the current `sleep` value for AC Power. Then:

   ```
   caffeinate -dimsu
   sudo pmset -c sleep 0
   ```

   The `-c` flag changes only the AC power setting, so battery behavior stays as it was. Run `caffeinate` in its own terminal. These settings are standard but UNVERIFIED on this machine, and step 6 checks them.

### Windows steps

The Windows steps are in `docs/windows-edge-setup.md`, section "Thunderbolt link to the Mac".

### Teardown / undo

Record the previous values before you change anything, because these commands restore what you recorded.

Mac:

```
sudo networksetup -setdhcp "Thunderbolt Bridge"
sudo pmset -c sleep <previous>
```

Use the `sleep` value you recorded from `pmset -g custom`. Stop `caffeinate` with Ctrl-C.

Windows (run in an administrator PowerShell, replacing `<name>` with the adapter name; the same steps are referenced from `docs/windows-edge-setup.md`):

```powershell
Remove-NetIPAddress -InterfaceAlias "<name>" -IPAddress 10.42.0.2 -Confirm:$false
Remove-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo (TB)"
Set-NetConnectionProfile -InterfaceAlias "<name>" -NetworkCategory Public
powercfg /change standby-timeout-ac <previous>
```

Then restore the lid close action on AC in Power Options and the two "Allow the computer to turn off this device to save power" checkboxes in Device Manager (Thunderbolt adapter and Wi-Fi adapter) to the values you recorded.

### Validation checklist

Run these steps in order when the cable arrives, using a checkout of this repository on both laptops. Copy `docs/thunderbolt-link-results-template.md` and fill it in as you go. Plan on about 90 minutes in total. If a step fails and its text says to go to the fallback ladder, stop and do that.

**Step 1. Inventory (5 min).**

- Mac: `system_profiler SPThunderboltDataType`
- Windows: run `winver`, then in PowerShell `Get-PnpDevice | ? FriendlyName -match "USB4|Thunderbolt"`, and check the laptop's specification sheet for USB4 or Thunderbolt 4 versus Thunderbolt 3.
- Pass: the Mac lists a Thunderbolt bus, and Windows 11 shows a USB4 or Thunderbolt controller.
- Fail: Windows has no Thunderbolt or USB4 port, or runs Windows 10. Go to rung 2 of the fallback ladder.

**Step 2. Plug in directly (2 min).** Connect a Thunderbolt or USB4 cable straight from one laptop to the other. No dock, no hub.

**Step 3. Is there a link? (5 min).**

- Mac: `networksetup -listallhardwareports` and `ifconfig bridge0`
- Windows: `Get-NetAdapter | ft Name,InterfaceDescription,Status,LinkSpeed`
- Pass: both sides show an interface that is up. On Windows it is a new adapter with status Up at several Gbps. On the Mac `ifconfig bridge0` shows `status: active`.
- Fail: reseat the cable, try the other Thunderbolt port, reboot Windows, and ask whether Group Policy disables USB4 networking. If it still fails, go to rung 2.

**Step 4. Addressing (5 min).** Apply the Mac steps and the Windows steps above.

- Pass: `route -n get 10.42.0.2` on the Mac shows the Thunderbolt interface, the Mac has no default route through it, and the Windows MTU on the cable adapter is 1500.
- Fail: fix the addresses before going on. If the static address does not stick on Windows, prove the link with the 169.254 addresses first.

**Step 5. Link quality (15 min).**

- Mac: `ping -c 600 10.42.0.2` (10 minutes). Windows: `ping -n 600 10.42.0.1`.
- Then run the repository check. Mac: `venv/bin/python scripts/check_fleet_link.py --role server`. Windows: `venv\Scripts\python.exe scripts\check_fleet_link.py --role client --peer 10.42.0.1 --bind 10.42.0.2 --soak 120`.
- Optional: `iperf3 -s -B 10.42.0.1` on the Mac, then `iperf3 -c 10.42.0.1 -t 10` and again with `-R` on Windows. Over 100 Mbit/s is plenty. Skip it if iperf3 is not installed.
- Pass: the pings show 0 percent loss, an average under 1 ms and a maximum under 10 ms. Both sides of `check_fleet_link.py` print PASS. The median WebSocket round trip is under 5 ms. The soak line says PASS, which means no lost frame and no gap between replies above 500 ms. These thresholds are our own targets. The real requirement is the 3 second fleet watchdog.
- Fail: any loss, or any gap above 500 ms. Go to rung 2.

**Step 6. Robustness (15 min).**

1. Unplug the cable for 10 seconds, then plug it back in. Note when the ping fails, how many seconds until the first good ping after replugging, whether Windows kept 10.42.0.2 (`Get-NetIPAddress -InterfaceAlias "<name>"`), and whether the Mac still lists 10.42.0.1 in `ifconfig` while the cable is out.
2. Repeat the unplug three times, and once while `check_fleet_link.py` is running.
3. Let the Windows screen turn off and lock the Mac screen. Confirm the ping continues.
4. Confirm the power settings from the Mac steps and the Windows steps are applied.

- Pass: the link recovers by itself after every replug within 30 seconds and the Windows address is kept.
- Fail: the Windows address is lost on replug. Re-apply it with the `New-NetIPAddress` command each time, and note this in the results. If the link does not come back at all, go to rung 2.

**Step 7. Coexistence with the Tellos (20 min).** Drones on the floor, propellers on, no takeoff.

1. Join each laptop's Wi-Fi to its own Tello.
2. Repeat the route checks from step 4.
3. On each laptop run `scripts/tello_hw_check.py --drones 192.168.10.1 --video-check --json`. At the same time run a ping soak over the cable. On the Mac use `ping -i 0.2 10.42.0.2` (`sudo` is needed only for intervals under 0.002 s, per `man ping`).
4. Pass: both checks pass, the cable shows no packet loss during the video, and on Windows the state and video packets arrive. That proves the firewall rules for UDP 8890 and 11111.
5. Fail: Windows does not receive state or video. Check the firewall rules, then retry.

**Step 8. Full fleet path with fake drones over the cable (20 min).** This is the important test. Use `FLIGHTRISK_FLEET_NET=ap` and the same `FLIGHTRISK_EDGE_TOKEN` on both laptops.

- Mac: start the ground station with `scripts/demo_ground.sh` (it binds `127.0.0.1,10.42.0.1`), then fake edge-1 over loopback: `python -m flightrisk.edge_device --source fake --drone-id drone-1 --ws-url ws://127.0.0.1:9000`
- Windows: start fake edge-2 with `--drone-id drone-2 --ws-url ws://10.42.0.1:9000`.
- In the dashboard run Plan, then Start. Pull the cable while the fake drones fly their lanes.
- Expected: edge-2 logs `edge_link_lost` and lands after about 3 seconds. The ground marks drone-2 lost after about 5 seconds and gives its lanes to drone-1. If 10.42.0.1 goes away (step 6), the ground logs `ground_bind_address_lost`, and `ground_bind_address_restored` after replugging; if macOS keeps the address on an inactive interface, neither line appears.
- Key observation: edge-1 is on `ws://127.0.0.1:9000`, so it should stay connected while the cable is out and keep flying or holding (UNVERIFIED on hardware; this step measures it). Confirm that it does. If it drops anyway, record that; a cable pull then lands both drones, which is safe but not graceful. Do not work around it by binding the ground station to `0.0.0.0`.
- After replugging, time how long edge-2 takes to rejoin. The reconnect delay doubles from 1 second up to a 2 second cap for fleet edges, so expect a rejoin within a few seconds.

**Step 9. Decide.** Choose a rung from the fallback ladder and write the decision in the results file.

### Windows firewall and Wi-Fi notes

Both laptops need inbound TCP 9000 from the peer once either one can host the ground station. The Windows Tello Wi-Fi also needs inbound UDP 8890 and 11111 from 192.168.10.1. If video or state packets do not arrive on Windows, allow Python through the firewall for that network profile. This is UNVERIFIED on our machines, and step 7 tests it.

### Fallback ladder

The protocol and addresses are the same on every rung. Only the cabling changes.

| Rung | Link | Use it when |
|---|---|---|
| 1 | Thunderbolt or USB4 cable, static 10.42.0.1 and 10.42.0.2 | steps 1 to 8 pass |
| 2 | Two USB-C gigabit Ethernet adapters and one Cat6 cable, same addresses (Path 2 above) | Windows has no Thunderbolt or USB4, or the link fails any step |
| 3 | Station mode: drones and laptops on one 2.4 GHz router (Path 1 above) | `tello_hw_check.py --station-mode` and `--video-check` both pass. Tello video may not work in station mode (UNVERIFIED for current EDU firmware) |
| 4 | One live Tello plus one fake edge (rung 2 in `docs/demo-script.md`) | there is no working link between the laptops at all |

## Launch

Run the launchers after exporting the settings and `FLIGHTRISK_EDGE_TOKEN`. Start the ground station first.

On the Mac, in one terminal for the ground station and another for edge-1:

```
scripts/demo_ground.sh
FLIGHTRISK_GROUND_LOCAL=1 DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh
```

(`FLIGHTRISK_GROUND_LOCAL=1` as a prefix on the command passes the variable to the launcher and makes edge-1 connect over loopback in AP mode. If you source `scripts/fleet_env.sh` yourself first, `export` the variable. Station mode ignores it.)

On Windows, for edge-2:

```
$env:DRONE_ID = "2"; $env:ALT_CM = "160"; .\scripts\demo_edge.ps1
```

Lay out the drones as in "Placement and heading" above first. The dashboard should show both drones with their clock offsets and round-trip times. Check each battery is at least 30 percent and the floor area is clear before takeoff.

Confirming a match on a fleet lands every drone in place with LAND ALL. The Tello has no return-to-home, so each drone lands wherever it is.

### Observability (optional)

To watch both laptops in Grafana, start the stack on the Mac and export these before running the launchers (details, and the cable bind for the Windows laptop, in `observability/README.md`, "Two laptops on the Thunderbolt cable"):

```
export FLIGHTRISK_OTEL_ENABLED=true OTEL_EXPORTER=otlp-grpc OTEL_ENDPOINT=http://127.0.0.1:4317
export OTEL_RESOURCE_ATTRIBUTES=service.instance.id=mac-ground FLIGHTRISK_TELEMETRY_CONTENT=1
```

On Windows set the same variables with `OTEL_ENDPOINT=http://10.42.0.1:4317` and `service.instance.id=edge-2`. `FLIGHTRISK_TELEMETRY_CONTENT=1` records what the operator typed (redacted, capped) and keeps a private copy of the target photo for the Grafana preview; it is off unless set. Secrets, embeddings, bystander crops, video frames and reasoning text are never recorded.

## Quick reference

| Setting | Station | AP |
|---|---|---|
| `FLIGHTRISK_FLEET_NET` | `station` | `ap` |
| `FLIGHTRISK_TELLO_HOST` | discovered drone IP, set by you | 192.168.10.1 |
| `FLIGHTRISK_EDGE_HOST` | Mac LAN IP | `127.0.0.1,10.42.0.1` |
| `FLIGHTRISK_EDGE_WS` | `ws://<mac lan ip>:9000` | `ws://10.42.0.1:9000`, or `ws://127.0.0.1:9000` when `FLIGHTRISK_GROUND_LOCAL=1` (Mac) |
| `FLIGHTRISK_GROUND_LOCAL` | ignored | exported as `1` on the Mac (ground station and edge-1), unset on Windows |
| `FLIGHTRISK_EDGE_TOKEN` | required, same on both | required, same on both |
| Tello ports | UDP 8889 commands, 8890 state, 11111 video | same |
