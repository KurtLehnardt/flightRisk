# Two standard Tellos from one Windows laptop

This note describes how one Windows laptop (the ZBook) flies two standard (non-EDU) Tello drones, one per Wi-Fi adapter, as two FlightRisk edge processes: edge-1 for drone-1 and edge-2 for drone-2. Both edges dial the ground station on the Mac over the Thunderbolt cable (`ws://10.42.0.1:9000`). Nothing here has been verified on hardware yet; the [hardware checklist](#hardware-verification-checklist) at the end is the plan for that.

When the new option is not given, an edge behaves exactly as before: it uses the djitellopy client unchanged and binds the usual wildcard sockets.

## Why the normal edge cannot do this

Both Tellos are at 192.168.10.1 on their own 192.168.10.0/24 networks. Standard Tellos have no station mode, so there is no router that could give them different addresses. The stock djitellopy 2.5.0 client cannot tell the two drones apart:

- One process-wide command socket is bound to all addresses on UDP 8889, and one state socket on UDP 8890.
- Command replies and state packets are filed by sender address in one module-level table, so two drones that are both 192.168.10.1 overwrite each other.
- Video is read from `udp://@0.0.0.0:11111`, so only one stream can be received per host.

For that reason `docs/fleet-setup.md` runs one edge per computer.

## The idea: one local IP per adapter

Windows on the ZBook uses the strong host model (WeakHostSend and WeakHostReceive are off, verified). If the two Wi-Fi adapters have different local IPs, for example 192.168.10.2 and 192.168.10.50, then a socket bound to one adapter's IP receives only the datagrams sent to that IP. Each Tello replies to, and streams to, the address that sent it `command` and `streamon`. So an edge whose sockets are all bound to one adapter's IP talks only to the drone on that adapter. Two sockets on the same port bound to two different local IPs coexist, but a wildcard bind on that port then fails (WinError 10048, verified on loopback). Therefore, in this mode no socket on 8889, 8890 or 11111 may ever bind 0.0.0.0, and every edge on the laptop must use the option.

## Transport design

`flightrisk/drone/tello_bound.py` adds `BoundTello`, a subclass of djitellopy's `Tello` that is used only when a local IP is given. It overrides every method that touches djitellopy's module-level sockets or tables, and nothing else:

- `__init__` does not call djitellopy's constructor (which would bind the shared wildcard socket). It opens its own command socket bound to `(local_ip, 8889)` and its own state socket bound to `(local_ip, 8890)`, each with a receiver thread, and keeps replies and state in a per-instance table.
- `send_command_with_return` and `send_command_without_return` send from the bound command socket, so every command, keepalive and `rc` leaves from the same source port, 8889. A Tello answers only the source port of the first `command` it received; this is the same rule the hardware check follows since PR #119.
- `get_frame_read` returns `BoundFrameRead` (see video below), and `end` lands and stops the stream like djitellopy's, then closes the sockets. It never touches djitellopy's module-level table.

All other client methods (`connect`, `streamon`, the state getters, `send_rc_control`, `parse_state`) are djitellopy's own and run unchanged. A test fails if a future djitellopy adds a method that uses the module-level socket or table and is not overridden.

Why a subclass rather than patching djitellopy or writing a new client: patching module globals would change the default path as well, and `TelloController` uses about twenty djitellopy methods (and reaches into the per-drone reply list and state entry). A subclass replaces only the five socket-facing methods, keeps djitellopy's command and state parsing, and leaves `TelloController`'s flight logic untouched: the takeoff reply handling of PR #131 (`_await_takeoff_reply`), the link-health preflight of PR #127 (`link_status`) and landing (which is never gated on link health) all run the same code against the per-instance reply list. The only controller change in this mode is that the state-arrival hook wraps the per-instance state entry instead of djitellopy's module-level one.

Receiver threads accept a datagram only when its source IP is the Tello host, as djitellopy does. A datagram from any other source is dropped, counted, and logged as `tello_foreign_packet` (throttled). Unlike djitellopy's threads, a receiver that hits an error on one datagram (for example a Windows `ConnectionResetError` after an ICMP port-unreachable) logs it and keeps running; it stops only when its socket is closed.

### Video bound to one adapter

`BoundFrameRead` opens its own UDP socket bound to `(local_ip, 11111)` and feeds the datagrams to PyAV's H.264 parser and decoder (`av.CodecContext.create("h264", "r")`, `parse`, `decode`), the decoder that ships with the installed PyAV. Frames are RGB arrays, as djitellopy's `np.array(frame.to_image())` gives. Stopping the reader closes its socket, so stream recovery (`streamoff` and `streamon`) can bind the port again at once.

The alternative, `av.open("udp://@<local_ip>:11111")`, was checked on macOS: FFmpeg binds the IP in the URL (not the wildcard). It was not chosen because FFmpeg's socket cannot be closed while a read blocks (so a restart can find the port still bound), because `av.open` blocks for up to 5 s per attempt, and because tests cannot inject a socket into it. Whether FFmpeg on Windows binds the same way is unverified.

## Interface

```
python -m flightrisk.edge_device --source tello --tello-local-ip 192.168.10.2 [--tello-ifindex 12] ...
```

- `--tello-local-ip IP` (fallback: the `FLIGHTRISK_TELLO_LOCAL_IP` environment variable). It must be an IPv4 address, not 0.0.0.0, broadcast or multicast. An explicit flag with a source other than `tello` is an error; the environment variable is ignored for other sources.
- `--tello-ifindex N` (fallback: `FLIGHTRISK_TELLO_IFINDEX`), optional and Windows only. It sets `IP_UNICAST_IF` (option 31, the interface index as a 4-byte network-byte-order value) on the command socket, so outgoing commands leave through that adapter even if the routing table would pick another. It needs `--tello-local-ip`. Off Windows the edge refuses it. When the system lists its interfaces (`socket.if_nameindex`), the index must be one of them.
- `TelloController(name, host, local_ip=None, ifindex=None)`. With `local_ip=None` (the default) the controller builds the stock djitellopy client exactly as before.
- `scripts/tello_hw_check.py --local-ip IP [--ifindex N]` runs the hardware check through one adapter, with the same no-wildcard rule.
- `scripts/demo_edge_pair.ps1` starts the two edges (see the runbook).

## Failure modes

The edge refuses to start, with a message that names the problem and the fix, when:

- The local IP is not assigned to any adapter on this computer (the adapter is missing, unplugged or not yet joined). This is checked by binding a throwaway socket to `(local_ip, 0)`, which works the same way on Windows, macOS and Linux.
- The local IP is on two adapters at once (an address conflict, for example both adapters on DHCP got 192.168.10.2 from their drones). Checked with `psutil`, which the edge already has through Ultralytics; if `psutil` is missing the check is skipped and logged.
- A port is already bound on that IP: another edge uses the same local IP, the hardware check is still running, or an edge without the option holds the wildcard. The message says which port and suggests these causes.
- `--tello-ifindex` is given off Windows, without a local IP, or names an interface that does not exist.

A Tello host outside the local IP's /24 network is logged as a warning, not refused.

While running, a reply or state packet that reaches the wrong adapter cannot be seen by the edge on the other adapter: under the strong host model Windows delivers it only to the socket bound to the IP it was sent to. If it reaches this edge's socket from a source other than the Tello host it is dropped and logged. One limit: two drones both send from 192.168.10.1, so if a misconfigured routing table delivered drone-2's packets to adapter 1, the edge could not tell them apart. The hardware checklist tests this directly.

## Reporting the adapter

- The `edge_source_tello` log line gains `tello_local_ip` and `tello_ifindex` (only in this mode), and the transport logs `tello_transport_bound` with the bound address of each socket.
- When the drone link is unhealthy, `link_status().detail`, which reaches the ground in the `drone_unreachable` nack and in telemetry as `drone_link_detail`, ends with `via <local_ip>`, for example `Tello unreachable (no state for 4 s) via 192.168.10.50`. The default mode's text is unchanged.

## Risks

- **CPU.** Two edges run two YOLO pipelines on one CPU. This is the most likely failure. A throwaway measurement on the development Mac (10 cores, Apple silicon, YOLO forced onto the CPU, 960 x 720 frames, ground station and dashboard on the same machine) gave about 20 frames a second for one edge alone and about 17 a second for each of two edges, each edge using about 1.7 cores; the ground and dashboard used about 0.05 cores. The ZBook's CPU will differ. Watch the per-edge frame rate and CPU, and if it is too slow, run one edge with a lighter detection profile or lower its frame rate.
- **Single point of failure.** One laptop, one power supply and one OS now carry both drones. A crash or sleep of the ZBook drops both links; each Tello then lands itself after 15 s without a command.
- **Radio placement.** A USB 3 adapter next to the internal 2.4 GHz radio can interfere with it. Put the USB adapter on an extension cable, away from the laptop.
- **Tello timers.** A Tello lands itself after 15 s without a command (each edge's keepalive runs every 10 s) and powers off after about 8 minutes idle on the ground. Whether the keepalive prevents the idle power-off is unverified; power the drones on shortly before the run.
- **Open question.** Drone-2 refused takeoff on the ZBook. It is not known whether the TP-Link adapter is the cause. The checklist swaps adapters to find out.

## ZBook runbook

1. Plug the TP-Link adapter in through a USB extension cable. Join drone-1's `TELLO-...` network with the internal Wi-Fi adapter and drone-2's with the TP-Link adapter.
2. List the adapters and their indexes:

   ```
   Get-NetIPAddress -AddressFamily IPv4 | Sort-Object InterfaceIndex | Format-Table InterfaceIndex, InterfaceAlias, IPAddress, PrefixLength
   ```

3. Give the TP-Link adapter a fixed address with no gateway, in an administrator PowerShell. Use an address the drone's DHCP is unlikely to hand out, such as 192.168.10.50, so it cannot clash with the internal adapter's DHCP address (which has been both .2 and .3 in the past). Replace `"Wi-Fi 2"` with the alias from step 2:

   ```
   Set-NetIPInterface -InterfaceAlias "Wi-Fi 2" -Dhcp Disabled
   New-NetIPAddress -InterfaceAlias "Wi-Fi 2" -IPAddress 192.168.10.50 -PrefixLength 24
   ```

   To undo it later: `Remove-NetIPAddress -InterfaceAlias "Wi-Fi 2" -IPAddress 192.168.10.50 -Confirm:$false` and `Set-NetIPInterface -InterfaceAlias "Wi-Fi 2" -Dhcp Enabled`.

4. Run step 2 again and note each adapter's IP and InterfaceIndex. The two IPs must differ.
5. Make sure the Mac runs only the ground station (no edge-1 there: the drone ids would clash) with `--fleet drone-1,drone-2`, and that the Thunderbolt link works (`docs/windows-edge-setup.md`, step 7).
6. Set the token in this PowerShell window (`$env:FLIGHTRISK_EDGE_TOKEN = "..."`, the same value as on the Mac), then start both edges, each in its own console window:

   ```
   .\scripts\demo_edge_pair.ps1 -LocalIp1 192.168.10.2 -LocalIp2 192.168.10.50 -IfIndex1 12 -IfIndex2 23
   ```

   Defaults: drone-1 at altitude slot 50 cm, drone-2 at 80 cm, ground `ws://10.42.0.1:9000`. `-DryRun` prints both command lines without starting anything. The script turns off QuickEdit in each edge window, so a stray click cannot freeze an edge, and each window also writes its output to `logs\edge-<drone id>-<time>.log` (`-NoLog` turns this off).

7. To share what happened, summarise the logs: `.\scripts\flight_log_summary.ps1 logs\edge-drone-1-*.log logs\edge-drone-2-*.log -Minutes 5`. The summary keeps start, refusal, takeoff, link-health and clock events, warnings and errors, removes tokens and long hex strings, and is never longer than 20 000 bytes, so it can be pasted into a message.

## Hardware verification checklist

Run on the ZBook, step by step, before any flight with two drones:

1. Both adapters have different IPs; `Get-NetIPAddress` shows each IP on exactly one adapter.
2. Per adapter: `venv\Scripts\python.exe scripts\tello_hw_check.py --drones 192.168.10.1 --local-ip <IP> --ifindex <N> --video-check` passes for each drone, one at a time and then with both drones on.
3. Start the edge pair. Each edge logs `tello_transport_bound` with its own IP, then `tello_connected` with that drone's battery.
4. Cross-talk test: power off drone-2 while both edges run. Within about 3 s edge-2's link must go unhealthy (`state_stale`, detail ending `via <IP2>`) while edge-1 stays healthy. Power drone-2 on again, restart edge-2, then repeat with drone-1.
5. Video: each dashboard tile shows its own drone's camera (cover one lens to tell them apart).
6. A wildcard bind fails while the edges run: a quick `python -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('0.0.0.0', 8889))"` must raise WinError 10048.
7. Takeoff and land each drone alone from the dashboard, then both together, at slots 50 and 80 cm.
8. CPU: watch both edges for 5 minutes with Task Manager; note the CPU and each edge's frame rate.
9. After any failure, run the log summary from runbook step 7 and keep its output with the notes of that step.
10. Open question: swap the adapters (drone-2 on the internal Wi-Fi, drone-1 on the TP-Link) and repeat step 7, to see whether the takeoff refusal follows the TP-Link adapter or the drone.
