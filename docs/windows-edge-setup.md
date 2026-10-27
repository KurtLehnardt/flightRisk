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

The script runs `py -3.13 -m venv venv`, then `venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt`, and downloads `yolo11n.pt`. It calls the venv python directly, which avoids the `Activate.ps1` execution policy block. It does not install Ollama and does not set up InsightFace models.

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

`scripts\demo_edge.ps1` starts the drone 2 edge process against `ws://10.42.0.1:9000` using the token from step 4.
