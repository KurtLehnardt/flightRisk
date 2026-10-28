#!/usr/bin/env python3
"""Tello EDU hardware check.

Run this by hand against real drones to decide the fleet network path
(station mode through a router, or plain AP mode with a wired laptop link).
It prints a PASS/FAIL table, or a stable JSON document with ``--json``.

Modes (they may be combined, except ``--station-mode``, which runs alone):

* ``--drones HOST[,HOST]``: send ``command``, ``sdk?``, ``sn?``, ``battery?``
  and ``wifi?`` to each host on UDP 8889. Defaults to 192.168.10.1.
* ``--discover SUBNET``: find drones that answer ``command`` on UDP 8889.
* ``--video-check``: ``streamon``, count video packets for 5 s, ``streamoff``,
  then probe ``port 8890 11112`` and report ``ok`` or ``unknown command``.
* ``--ap-mode-check``: verify plain AP mode round-trips.
* ``--fleet-net ap --link-role server|client --peer IP``: ping the peer and do
  a WebSocket round-trip on port 9000, reporting the median RTT.
* ``--station-mode SSID PASS --i-understand-this-reconfigures-the-drone``:
  sends ``ap`` to the single drone currently joined. This reconfigures the
  drone and is never done without the explicit acknowledgement flag.

The exit status is 0 when every check passes and 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DEFAULT_DRONE_HOST = "192.168.10.1"
CMD_PORT = 8889
VIDEO_PORT = 11111
STATE_PORT = 8890
ALT_VIDEO_PORT = 11112
LINK_PORT = 9000
DEFAULT_TIMEOUT = 3.0
VIDEO_SECONDS = 5.0
WS_PINGS = 8
MAX_DISCOVER_HOSTS = 1024
ACK_FLAG = "--i-understand-this-reconfigures-the-drone"
SCHEMA_VERSION = 1

PASS = "PASS"
FAIL = "FAIL"
INFO = "INFO"

STATUS_COMMANDS = ("command", "sdk?", "sn?", "battery?", "wifi?")
OPTIONAL_COMMANDS = ("sdk?", "sn?")


@dataclass
class CheckResult:
    """One row of the report. ``status`` is PASS, FAIL or INFO."""

    name: str
    target: str
    status: str
    detail: str = ""
    value: Any = None
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# UDP helpers
# --------------------------------------------------------------------------

def _clean(reply: bytes) -> str:
    return reply.decode("utf-8", errors="replace").strip().strip("\x00").strip()


# One UDP socket per (host, port), reused for every command. A real Tello answers only the
# source port that sent it ``command``, so a new socket per command gets no reply after the first.
# For the real command port the socket binds local UDP 8889, the port djitellopy (the dashboard
# and the edge) always sends from, so a later FlightRisk process is not locked out by this check.
_CMD_SOCKETS: dict[tuple[str, int], socket.socket] = {}


def _command_socket(host: str, port: int) -> socket.socket:
    key = (host, port)
    sock = _CMD_SOCKETS.get(key)
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if port == CMD_PORT:
            try:
                sock.bind(("", CMD_PORT))
            except OSError:
                pass  # port busy (another Tello program is running): fall back to any free port
        _CMD_SOCKETS[key] = sock
    return sock


# A real Tello can send duplicate or late "ok" replies, and a reply that is not text. Those are skipped
# (so they are not read as the answer to the next command) and listed per host in one INFO row.
_STRAYS: dict[str, list[str]] = {}


def close_command_sockets() -> None:
    """Close the shared command sockets (call once the report is done)."""
    for sock in _CMD_SOCKETS.values():
        try:
            sock.close()
        except OSError:
            pass
    _CMD_SOCKETS.clear()
    _STRAYS.clear()


def _is_text(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(ch.isprintable() or ch in "\r\n\t\x00" for ch in text)


def _note_stray(host: str, data: bytes) -> None:
    shown = _clean(data) if _is_text(data) else "binary " + data[:8].hex()
    _STRAYS.setdefault(host, []).append(shown)


def _drain(sock: socket.socket, host: str) -> None:
    """Drop late replies to earlier commands so they are not read as the next reply."""
    sock.settimeout(0.0)
    try:
        while True:
            data, _ = sock.recvfrom(2048)
            _note_stray(host, data)
    except (BlockingIOError, socket.timeout, OSError):
        pass


def send_command(
    host: str,
    command: str,
    *,
    port: int = CMD_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Send one SDK command and return the stripped reply, or None on timeout.

    Every command to the same host and port goes out from the same local UDP port. Replies that are not
    text are skipped, and so is a bare "ok" when the command is a query (ends with "?"), because that is a
    duplicate or late acknowledgement and not the value asked for. Skipped replies are noted per host.
    """
    sock = _command_socket(host, port)
    wants_value = command.strip().endswith("?")
    try:
        _drain(sock, host)
        sock.sendto(command.encode("utf-8"), (host, port))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            data, _ = sock.recvfrom(2048)
            text = _clean(data)
            if not _is_text(data) or (wants_value and text.lower() == "ok"):
                _note_stray(host, data)
                continue
            return text
    except socket.timeout:
        return None
    except OSError:
        # A broken socket is dropped so the next command starts a fresh one.
        _CMD_SOCKETS.pop((host, port), None)
        try:
            sock.close()
        except OSError:
            pass
        return None


def _is_error(reply: str) -> bool:
    low = reply.lower()
    return low.startswith("error") or low.startswith("unknown command")


# --------------------------------------------------------------------------
# Individual checks
# --------------------------------------------------------------------------

def check_drone(host: str, *, port: int = CMD_PORT, timeout: float = DEFAULT_TIMEOUT) -> list[CheckResult]:
    """Run the five status commands against one drone."""
    results: list[CheckResult] = []
    _STRAYS.pop(host, None)
    for cmd in STATUS_COMMANDS:
        reply = send_command(host, cmd, port=port, timeout=timeout)
        name = f"cmd:{cmd}"
        if reply is None:
            results.append(CheckResult(name, host, FAIL, "no reply (timeout)"))
        elif cmd in OPTIONAL_COMMANDS and reply.lower().startswith("unknown command"):
            # sdk? and sn? exist only on a Tello EDU (or newer firmware); no flight needs them.
            results.append(CheckResult(name, host, INFO, f"not supported by this drone: {reply}", reply))
        elif _is_error(reply):
            results.append(CheckResult(name, host, FAIL, f"drone replied: {reply}", reply))
        elif cmd == "command" and reply.lower() != "ok":
            results.append(CheckResult(name, host, FAIL, f"expected ok, got: {reply}", reply))
        elif cmd == "battery?" and not _is_percent(reply):
            results.append(CheckResult(name, host, FAIL, f"battery not 0-100: {reply}", reply))
        else:
            results.append(CheckResult(name, host, PASS, "", reply))
    strays = _STRAYS.get(host)
    if strays:
        shown = ", ".join(repr(s) for s in strays[:6])
        results.append(CheckResult("stray-replies", host, INFO, f"ignored {len(strays)} duplicate or non-text reply(ies): {shown}", strays[:6]))
    return results


def _is_percent(text: str) -> bool:
    try:
        return 0 <= int(text) <= 100
    except ValueError:
        return False


def discover(
    subnet: str,
    *,
    port: int = CMD_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CheckResult]:
    """Send ``command`` to every host in ``subnet`` and list the ones that answer."""
    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as exc:
        return [CheckResult("discover", subnet, FAIL, f"invalid subnet: {exc}")]
    if net.version != 4:
        return [CheckResult("discover", subnet, FAIL, "only IPv4 subnets are supported")]
    hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
    if len(hosts) > MAX_DISCOVER_HOSTS:
        return [CheckResult("discover", subnet, FAIL, f"subnet has {len(hosts)} hosts, limit is {MAX_DISCOVER_HOSTS}")]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, str] = {}
    try:
        sock.settimeout(0.2)
        for host in hosts:
            try:
                sock.sendto(b"command", (host, port))
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if _clean(data).lower() == "ok":
                found[addr[0]] = "ok"
    finally:
        sock.close()

    if not found:
        return [CheckResult("discover", subnet, FAIL, f"no drone answered command on UDP {port}", [])]
    ips = sorted(found, key=lambda s: ipaddress.ip_address(s))
    return [CheckResult("discover", subnet, PASS, f"{len(ips)} drone(s) found", ips)]


def probe_port(host: str, *, port: int = CMD_PORT, timeout: float = DEFAULT_TIMEOUT) -> CheckResult:
    """Probe whether the drone accepts ``port 8890 11112``. Informational only."""
    reply = send_command(host, f"port {STATE_PORT} {ALT_VIDEO_PORT}", port=port, timeout=timeout)
    if reply is None:
        return CheckResult("port-probe", host, FAIL, "no reply (timeout)")
    if reply.lower() == "ok":
        # Restore the default video port so the drone is left as it was found.
        send_command(host, f"port {STATE_PORT} {VIDEO_PORT}", port=port, timeout=timeout)
        return CheckResult("port-probe", host, INFO, "port command accepted", "ok")
    if "unknown command" in reply.lower():
        return CheckResult("port-probe", host, INFO, "port command not supported", "unknown command")
    return CheckResult("port-probe", host, INFO, f"unexpected reply: {reply}", reply)


def video_check(
    host: str,
    *,
    port: int = CMD_PORT,
    video_port: int = VIDEO_PORT,
    seconds: float = VIDEO_SECONDS,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CheckResult]:
    """``streamon``, count video packets for ``seconds``, ``streamoff``, then probe ``port``."""
    results: list[CheckResult] = []
    vsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        vsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            vsock.bind(("0.0.0.0", video_port))
        except OSError as exc:
            return [CheckResult("video", host, FAIL, f"cannot bind UDP {video_port}: {exc}")]
        vsock.settimeout(0.2)

        for pre in ("command", "streamon"):
            reply = send_command(host, pre, port=port, timeout=timeout)
            if reply is None or reply.lower() != "ok":
                results.append(CheckResult("video", host, FAIL, f"{pre} failed: {reply or 'no reply (timeout)'}"))
                return results

        packets = 0
        frames = 0
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                data, _ = vsock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            packets += 1
            # Tello fragments each frame into 1460-byte datagrams; a shorter one ends the frame.
            if len(data) != 1460:
                frames += 1
        send_command(host, "streamoff", port=port, timeout=timeout)
        status = PASS if packets > 0 else FAIL
        detail = f"{packets} packets, {frames} frames in {seconds:g} s"
        results.append(CheckResult("video", host, status, detail, {"packets": packets, "frames": frames}))
    finally:
        vsock.close()

    results.append(probe_port(host, port=port, timeout=timeout))
    return results


def ap_mode_check(host: str, *, port: int = CMD_PORT, timeout: float = DEFAULT_TIMEOUT) -> list[CheckResult]:
    """Verify plain AP mode: the drone answers ``command`` and ``sn?`` at ``host``."""
    results: list[CheckResult] = []
    for cmd in ("command", "sn?"):
        reply = send_command(host, cmd, port=port, timeout=timeout)
        if reply is None:
            results.append(CheckResult(f"ap:{cmd}", host, FAIL, "no reply (timeout)"))
        elif _is_error(reply):
            results.append(CheckResult(f"ap:{cmd}", host, FAIL, f"drone replied: {reply}", reply))
        else:
            results.append(CheckResult(f"ap:{cmd}", host, PASS, "", reply))
    return results


def station_mode(
    host: str,
    ssid: str,
    password: str,
    *,
    acknowledged: bool,
    port: int = CMD_PORT,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[CheckResult]:
    """Send ``ap SSID PASS`` to ``host``. Refuses unless ``acknowledged`` is True."""
    if not acknowledged:
        raise PermissionError(f"station mode reconfigures the drone and requires {ACK_FLAG}")
    if any(c in ssid + password for c in "\r\n"):
        return [CheckResult("station-mode", host, FAIL, "ssid or password contains a newline")]
    reply = send_command(host, "command", port=port, timeout=timeout)
    if reply is None or reply.lower() != "ok":
        return [CheckResult("station-mode", host, FAIL, f"command failed: {reply or 'no reply (timeout)'}")]
    reply = send_command(host, f"ap {ssid} {password}", port=port, timeout=timeout)
    if reply is None:
        return [CheckResult("station-mode", host, FAIL, "no reply to ap (timeout)")]
    if reply.lower().startswith("ok"):
        return [CheckResult("station-mode", host, PASS, "ap accepted; the drone will reboot into station mode", reply)]
    return [CheckResult("station-mode", host, FAIL, f"ap rejected: {reply}", reply)]


# --------------------------------------------------------------------------
# Fleet link check (ping + WebSocket)
# --------------------------------------------------------------------------

def system_ping(host: str, count: int = 3, timeout: float = 2.0) -> tuple[bool, str]:
    """Ping ``host`` with the system ping binary. ``host`` must be an IP address."""
    ip = str(ipaddress.ip_address(host))
    if platform.system() == "Windows":
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), ip]
    elif platform.system() == "Darwin":
        cmd = ["ping", "-c", str(count), "-W", str(int(timeout * 1000)), ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, int(timeout))), ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=count * (timeout + 1) + 5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return proc.returncode == 0, "reply received" if proc.returncode == 0 else "no reply"


async def echo_server(
    bind: str,
    port: int,
    *,
    stop: "asyncio.Event",
    ready: Optional[Callable[[int], None]] = None,
) -> None:
    """Run a WebSocket echo listener until ``stop`` is set."""
    import websockets

    async def handler(ws):
        async for message in ws:
            await ws.send(message)

    async with websockets.serve(handler, bind, port) as server:
        bound = list(server.sockets)[0].getsockname()[1]
        if ready:
            ready(bound)
        await stop.wait()


async def ws_roundtrip(url: str, *, pings: int = WS_PINGS, timeout: float = DEFAULT_TIMEOUT) -> list[float]:
    """Send ``pings`` text frames and return each round-trip time in milliseconds."""
    import websockets

    rtts: list[float] = []
    async with websockets.connect(url, open_timeout=timeout, ping_interval=None) as ws:
        for i in range(pings):
            start = time.perf_counter()
            await ws.send("ping")
            reply = await asyncio.wait_for(ws.recv(), timeout)
            if reply != "ping":
                raise RuntimeError(f"unexpected echo on frame {i}: {reply!r}")
            rtts.append((time.perf_counter() - start) * 1000.0)
    return rtts


def link_check_client(
    peer: str,
    *,
    port: int = LINK_PORT,
    timeout: float = DEFAULT_TIMEOUT,
    ping_fn: Callable[[str], tuple[bool, str]] = system_ping,
) -> list[CheckResult]:
    """Ping the peer and do a WebSocket round-trip, reporting the median RTT."""
    results: list[CheckResult] = []
    ok, detail = ping_fn(peer)
    results.append(CheckResult("link:ping", peer, PASS if ok else FAIL, detail))
    url = f"ws://{peer}:{port}"
    try:
        rtts = asyncio.run(ws_roundtrip(url, timeout=timeout))
        median = round(statistics.median(rtts), 3)
        results.append(CheckResult("link:websocket", url, PASS, f"median RTT {median} ms over {len(rtts)} frames", median))
    except Exception as exc:  # network errors vary widely; report them all as FAIL
        results.append(CheckResult("link:websocket", url, FAIL, f"{type(exc).__name__}: {exc}"))
    return results


def link_check_server(
    bind: str,
    peer: str,
    *,
    port: int = LINK_PORT,
    serve_seconds: float = 60.0,
    ping_fn: Callable[[str], tuple[bool, str]] = system_ping,
    ready: Optional[Callable[[int], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> list[CheckResult]:
    """Start the echo listener, ping the peer, then serve until ``serve_seconds`` pass."""
    results: list[CheckResult] = []

    async def run() -> None:
        stop = asyncio.Event()
        started = asyncio.Event()
        bound_port: list[int] = []

        def on_ready(p: int) -> None:
            bound_port.append(p)
            started.set()
            if ready:
                ready(p)

        task = asyncio.create_task(echo_server(bind, port, stop=stop, ready=on_ready))
        try:
            await asyncio.wait_for(started.wait(), 10)
        except asyncio.TimeoutError:
            task.cancel()
            results.append(CheckResult("link:listen", f"{bind}:{port}", FAIL, "listener did not start"))
            return
        results.append(CheckResult("link:listen", f"{bind}:{bound_port[0]}", PASS, "echo listener started"))
        ok, detail = await asyncio.to_thread(ping_fn, peer)
        results.append(CheckResult("link:ping", peer, PASS if ok else FAIL, detail))
        deadline = time.monotonic() + serve_seconds
        while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
            await asyncio.sleep(0.1)
        stop.set()
        await task

    try:
        asyncio.run(run())
    except OSError as exc:
        results.append(CheckResult("link:listen", f"{bind}:{port}", FAIL, f"cannot listen: {exc}"))
    return results


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def overall_status(results: list[CheckResult]) -> str:
    """FAIL if any check failed or nothing ran; INFO rows never fail the run."""
    if not results:
        return FAIL
    return FAIL if any(r.status == FAIL for r in results) else PASS


def to_json(results: list[CheckResult]) -> str:
    """Stable JSON: fixed schema, fixed key order, rows in run order."""
    doc = {
        "schema": SCHEMA_VERSION,
        "overall": overall_status(results),
        "checks": [
            {
                "name": r.name,
                "target": r.target,
                "status": r.status,
                "detail": r.detail,
                "value": r.value,
            }
            for r in results
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True)


def format_table(results: list[CheckResult]) -> str:
    rows = [("STATUS", "CHECK", "TARGET", "VALUE / DETAIL")]
    for r in results:
        value = "" if r.value is None else str(r.value)
        text = value if not r.detail else (f"{value} ({r.detail})" if value else r.detail)
        rows.append((r.status, r.name, r.target, text))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = []
    for row in rows:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(3)) + "  " + row[3])
    lines.append("")
    lines.append(f"OVERALL: {overall_status(results)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Tello EDU hardware check (PASS/FAIL report).")
    p.add_argument("--drones", metavar="HOST[,HOST]", help="run the status commands on each host")
    p.add_argument("--discover", metavar="SUBNET", help="find drones answering command on UDP 8889, e.g. 192.168.1.0/24")
    p.add_argument("--video-check", action="store_true", help="count video packets for 5 s, then probe 'port 8890 11112'")
    p.add_argument("--ap-mode-check", action="store_true", help="verify plain AP mode round-trips")
    p.add_argument("--station-mode", nargs=2, metavar=("SSID", "PASS"), help="switch the joined drone to station mode (needs the acknowledgement flag)")
    p.add_argument(ACK_FLAG, dest="ack", action="store_true", help="acknowledge that --station-mode reconfigures the drone")
    p.add_argument("--fleet-net", choices=["ap"], help="run the wired fleet link check (AP mode path)")
    p.add_argument("--link-role", choices=["server", "client"], help="role for --fleet-net")
    p.add_argument("--peer", metavar="IP", help="peer IP address for --fleet-net")
    p.add_argument("--bind", metavar="IP", default="10.42.0.1", help="server-role listen address (default 10.42.0.1)")
    p.add_argument("--link-port", type=int, default=LINK_PORT, help="WebSocket port for the link check (default 9000)")
    p.add_argument("--serve-seconds", type=float, default=60.0, help="server role: how long to keep the listener up")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-command timeout in seconds")
    p.add_argument("--video-seconds", type=float, default=VIDEO_SECONDS, help=argparse.SUPPRESS)
    p.add_argument("--cmd-port", type=int, default=CMD_PORT, help=argparse.SUPPRESS)
    p.add_argument("--video-port", type=int, default=VIDEO_PORT, help=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", help="print machine-readable JSON instead of the table")
    return p


def _hosts(arg: Optional[str]) -> list[str]:
    if not arg:
        return [DEFAULT_DRONE_HOST]
    return [h.strip() for h in arg.split(",") if h.strip()]


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[CheckResult]:
    # Validate everything that could reconfigure a drone before any packet is sent.
    if args.station_mode:
        if not args.ack:
            parser.error(f"--station-mode reconfigures the drone; add {ACK_FLAG} to confirm")
        hosts = _hosts(args.drones)
        if len(hosts) != 1:
            parser.error("--station-mode applies to the single drone currently joined; pass at most one host")
        other = [args.discover, args.video_check, args.ap_mode_check, args.fleet_net]
        if any(other):
            parser.error("--station-mode must be run on its own")
    elif args.ack:
        parser.error(f"{ACK_FLAG} only applies together with --station-mode")
    if args.fleet_net and (not args.link_role):
        parser.error("--fleet-net requires --link-role")
    if args.link_role and not args.fleet_net:
        parser.error("--link-role requires --fleet-net ap")
    if args.fleet_net and args.link_role == "client" and not args.peer:
        parser.error("--link-role client requires --peer")
    if args.peer:
        try:
            ipaddress.ip_address(args.peer)
        except ValueError:
            parser.error(f"--peer must be an IP address, got {args.peer!r}")

    if args.station_mode:
        ssid, password = args.station_mode
        return station_mode(
            _hosts(args.drones)[0], ssid, password,
            acknowledged=args.ack, port=args.cmd_port, timeout=args.timeout,
        )

    results: list[CheckResult] = []
    explicit_mode = any([args.discover, args.video_check, args.ap_mode_check, args.fleet_net])
    if args.drones or not explicit_mode:
        for host in _hosts(args.drones):
            results.extend(check_drone(host, port=args.cmd_port, timeout=args.timeout))
    if args.discover:
        results.extend(discover(args.discover, port=args.cmd_port, timeout=args.timeout))
    if args.ap_mode_check:
        for host in _hosts(args.drones):
            results.extend(ap_mode_check(host, port=args.cmd_port, timeout=args.timeout))
    if args.video_check:
        for host in _hosts(args.drones):
            results.extend(video_check(
                host, port=args.cmd_port, video_port=args.video_port,
                seconds=args.video_seconds, timeout=args.timeout,
            ))
    if args.fleet_net:
        if args.link_role == "client":
            results.extend(link_check_client(args.peer, port=args.link_port, timeout=args.timeout))
        else:
            results.extend(link_check_server(
                args.bind, args.peer or "10.42.0.2",
                port=args.link_port, serve_seconds=args.serve_seconds,
            ))
    return results


def main(argv: Optional[list[str]] = None) -> int:
    # A drone reply can hold bytes that are not valid text (shown as U+FFFD). With stdout redirected to a file
    # or pipe on Windows the encoding is cp1252, and printing that character used to crash the whole report.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        results = run(args, parser)
    finally:
        close_command_sockets()
    print(to_json(results) if args.json else format_table(results))
    return 0 if overall_status(results) == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
