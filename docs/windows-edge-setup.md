# Windows edge setup (drone 2)

The Windows laptop runs the edge process for drone 2. The Mac runs the ground station and the edge process for drone 1. The edge role does not need Ollama or InsightFace.

## 1. Install Python 3.13

Python 3.13 is required because onnxruntime has no wheels for newer versions yet (`choco install python3` currently gives 3.15). In an administrator PowerShell with Chocolatey installed:

```powershell
choco install python313 -y
```

## 2. Install the project

```powershell
git clone https://github.com/KurtLehnardt/flightRisk.git
cd flightRisk
powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
```

The script runs `py -3.13 -m venv venv`, then `venv\Scripts\python.exe -m pip install -r requirements-edge.txt -r requirements-dev.txt`, and downloads `yolo11n.pt`. It calls the venv python directly, which avoids the `Activate.ps1` execution policy block. It installs `requirements-edge.txt`, not `requirements.txt`, so InsightFace, Ollama, open_clip and MediaPipe are never installed, and it does not set up their models.

## 3. Wired link to the Mac (AP-path)

In AP mode the two laptops share a dedicated wired subnet, 10.42.0.0/24, with no gateway and no DNS. The Mac is 10.42.0.1 and this laptop is 10.42.0.2. Find the adapter name with `Get-NetAdapter` (typically "Ethernet"), then in an administrator PowerShell:

```powershell
New-NetIPAddress -InterfaceAlias "Ethernet" -IPAddress 10.42.0.2 -PrefixLength 24
Set-NetConnectionProfile -InterfaceAlias "Ethernet" -NetworkCategory Private
New-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Action Allow
New-NetFirewallRule -DisplayName "FlightRisk ground 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow
```

The ICMP rule lets the Mac ping this laptop during the link check. The TCP 9000 rule is only used if the ground station is moved to this laptop. In the default layout this laptop only needs outbound TCP to 10.42.0.1:9000, which Windows Defender Firewall allows by default.

## 4. Edge token

The ground station refuses a non-loopback bind without a token, so set the same `FLIGHTRISK_EDGE_TOKEN` on both hosts. In PowerShell:

```powershell
$env:FLIGHTRISK_EDGE_TOKEN = "<the same secret as on the Mac>"
```

Use `setx FLIGHTRISK_EDGE_TOKEN "<secret>"` to persist it for future terminals.

## 5. Check the link

With the ground station side started on the Mac, run on this laptop:

```powershell
venv\Scripts\python.exe scripts\check_fleet_link.py --help
```

Then run `scripts\check_fleet_link.py` with the options shown in the help text to ping 10.42.0.1 and open `ws://10.42.0.1:9000`.

## 6. Start the edge process

```powershell
powershell -ExecutionPolicy Bypass -File scripts\demo_edge.ps1
```

`scripts\demo_edge.ps1` starts the drone 2 edge process against `ws://10.42.0.1:9000` (leave `FLIGHTRISK_GROUND_LOCAL` unset here, because the ground station is on the Mac) using the token from step 4.

## 7. Thunderbolt link to the Mac (optional, replaces the Ethernet cable in step 3)

This is Path 3 in `docs/fleet-setup.md`, which has the validation checklist and the fallback ladder. It is UNVERIFIED on this laptop. Windows 11 has built-in Ethernet over USB4 that works with a Thunderbolt peer (Microsoft documentation), but Mac to Windows on this hardware has not been tested. Windows 10 is not supported for this path. The address plan is the same as step 3: Mac 10.42.0.1, this laptop 10.42.0.2, subnet 10.42.0.0/24, no gateway.

1. Check that the laptop has a USB4 or Thunderbolt port, and note the Windows version. In PowerShell:

   ```powershell
   winver
   Get-PnpDevice | ? FriendlyName -match "USB4|Thunderbolt"
   ```

   If there is no USB4 or Thunderbolt controller, or Windows is version 10, use the Ethernet path (step 3) instead.

2. Plug a Thunderbolt or USB4 cable straight into the Mac. No dock, no hub. Find the new adapter:

   ```powershell
   Get-NetAdapter | ft Name,InterfaceDescription,Status,LinkSpeed
   ```

   The adapter exists only while the cable is plugged in. Windows recreates it on every replug (UNVERIFIED: this comes from a single blog report), so check after each replug that it still has 10.42.0.2.

3. In an administrator PowerShell, replace `<name>` with the adapter name from step 2. Set the address with no gateway, set the MTU to 1500 to match the Mac, and mark the link Private:

   ```powershell
   New-NetIPAddress -InterfaceAlias "<name>" -IPAddress 10.42.0.2 -PrefixLength 24
   Set-NetIPInterface -InterfaceAlias "<name>" -NlMtuBytes 1500
   Set-NetConnectionProfile -InterfaceAlias "<name>" -NetworkCategory Private
   if (-not (Get-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo (TB)" -ErrorAction SilentlyContinue)) {
       New-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo (TB)" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Profile Private -RemoteAddress 10.42.0.0/24 -Action Allow
   }
   ```

   The check keeps the rule from being created twice if you run this step again. The rule is separate from the "FlightRisk ICMPv4 echo" rule in step 3, so it has its own name. It allows ping only from the 10.42.0.0/24 subnet and only on the Private profile. Whether `-RemoteAddress` scoping behaves as expected on this laptop is UNVERIFIED.

   If you want to keep the link on the Public profile instead, use this command in place of the `Set-NetConnectionProfile` line and the rule above:

   ```powershell
   New-NetFirewallRule -DisplayName "FlightRisk ICMPv4 echo (TB)" -Direction Inbound -Protocol ICMPv4 -IcmpType 8 -Profile Public -RemoteAddress 10.42.0.1 -Action Allow
   ```

   TCP 9000 is only needed if the ground station moves to this laptop. A rule for TCP 9100 is not needed yet, because nothing on main listens on 9100. It is for a later pull request. Do not turn on Internet Connection Sharing.

4. Check the route and the link:

   ```powershell
   route print
   Find-NetRoute -RemoteIPAddress 10.42.0.1   # UNVERIFIED on this laptop
   ping -n 4 10.42.0.1
   ```

   The route to 10.42.0.1 must use the Thunderbolt adapter. Nothing on the cable may be the default route.

5. The Tello Wi-Fi needs inbound UDP 8890 (state) and 11111 (video) from 192.168.10.1. If `scripts\tello_hw_check.py --drones 192.168.10.1 --video-check` shows no state or video packets, allow Python through Windows Defender Firewall for that network profile.

6. Keep the laptop awake and the adapters powered. Record before you change: note the current standby timeout (`powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`), the lid close action on AC, and the state of both Device Manager checkboxes, so that you can restore them. In an administrator PowerShell run `powercfg /change standby-timeout-ac 0`, set the lid close action on AC to "Do nothing" in Power Options, and in Device Manager clear "Allow the computer to turn off this device to save power" on both the Thunderbolt adapter and the Wi-Fi adapter. These steps are standard but UNVERIFIED on this laptop. To undo them, see "Teardown / undo" in Path 3 of `docs/fleet-setup.md`.

7. With the server side running on the Mac, check the link and soak it for two minutes:

   ```powershell
   venv\Scripts\python.exe scripts\check_fleet_link.py --role client --peer 10.42.0.1 --bind 10.42.0.2 --soak 120
   ```

   Pass means the ping and the WebSocket print PASS, and the soak line says PASS: no lost frame and no gap between replies above 500 ms.

Record the results in a copy of `docs/thunderbolt-link-results-template.md`.
