#!/usr/bin/env python3
"""Check the wired laptop-to-laptop link used by the AP-mode fleet path.

Run the server role on the Mac (10.42.0.1) and the client role on the Windows
laptop (10.42.0.2). Both sides ping the peer and then exchange eight JSON ping
frames over a WebSocket on port 9000. Each side prints PASS or FAIL for the
ping and for the WebSocket, plus the median round-trip time on the client.

    python scripts/check_fleet_link.py --role server
    python scripts/check_fleet_link.py --role client --peer 10.42.0.1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import subprocess
import sys
import time

DEFAULT_PORT = 9000
SERVER_ADDR = "10.42.0.1"
CLIENT_ADDR = "10.42.0.2"
PING_FRAMES = 8


def ping_host(host: str, timeout_s: float = 3.0) -> bool:
    """Send one ICMP echo request with the platform's ping command."""
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    elif platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, int(timeout_s))), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_s + 5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


async def run_server(bind: str, port: int, duration_s: float | None) -> bool:
    """Answer ping frames with pong frames until the duration ends or Ctrl-C."""
    import websockets

    async def handler(ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") == "ping":
                t2 = time.monotonic()
                await ws.send(json.dumps(
                    {"type": "pong", "t1": msg.get("t1"), "t2": t2, "t3": time.monotonic()}
                ))

    async with websockets.serve(handler, bind, port):
        print(f"server: WebSocket echo listening on ws://{bind}:{port}")
        if duration_s is None:
            await asyncio.Future()
        else:
            await asyncio.sleep(duration_s)
    return True


async def ws_round_trips(url: str, frames: int = PING_FRAMES, timeout_s: float = 5.0) -> list[float]:
    """Send ping frames and return the round-trip time of each reply in milliseconds."""
    import websockets

    rtts: list[float] = []
    async with websockets.connect(url, open_timeout=timeout_s) as ws:
        for _ in range(frames):
            t1 = time.monotonic()
            await ws.send(json.dumps({"type": "ping", "t1": t1}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout_s))
            t4 = time.monotonic()
            if reply.get("type") != "pong":
                raise ValueError(f"unexpected reply: {reply!r}")
            rtts.append(((t4 - t1) - (reply["t3"] - reply["t2"])) * 1000.0)
    return rtts


def run_client(peer: str, port: int) -> bool:
    ping_ok = ping_host(peer)
    print(f"ping {peer}: {'PASS' if ping_ok else 'FAIL'}")
    try:
        rtts = asyncio.run(ws_round_trips(f"ws://{peer}:{port}"))
    except Exception as exc:  # any connection failure is a FAIL, not a crash
        print(f"websocket ws://{peer}:{port}: FAIL ({type(exc).__name__}: {exc})")
        return False
    print(f"websocket ws://{peer}:{port}: PASS ({len(rtts)} frames, median RTT {statistics.median(rtts):.2f} ms)")
    return ping_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the wired fleet link (AP-mode path).")
    parser.add_argument("--role", required=True, choices=["server", "client"])
    parser.add_argument("--peer", help="peer IP address (default: the other laptop's fixed address)")
    parser.add_argument("--bind", default=SERVER_ADDR, help="server role: address to listen on")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--duration", type=float, default=None,
                        help="server role: stop after this many seconds (default: run until Ctrl-C)")
    args = parser.parse_args(argv)

    if args.role == "client":
        return 0 if run_client(args.peer or SERVER_ADDR, args.port) else 1

    peer = args.peer or CLIENT_ADDR
    ping_ok = ping_host(peer)
    print(f"ping {peer}: {'PASS' if ping_ok else 'FAIL'}")
    try:
        asyncio.run(run_server(args.bind, args.port, args.duration))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"websocket listener on {args.bind}:{args.port}: FAIL ({exc})")
        return 1
    return 0 if ping_ok else 1


if __name__ == "__main__":
    sys.exit(main())
