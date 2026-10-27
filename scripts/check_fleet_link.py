#!/usr/bin/env python3
"""Check the wired laptop-to-laptop link used by the AP-mode fleet path.

Run the server role on the Mac (10.42.0.1) and the client role on the Windows
laptop (10.42.0.2). Both sides ping the peer and then exchange eight JSON ping
frames over a WebSocket on port 9000. Each side prints PASS or FAIL for the
ping and for the WebSocket, plus the median round-trip time on the client.

    python scripts/check_fleet_link.py --role server
    python scripts/check_fleet_link.py --role client --peer 10.42.0.1

Options:
    --bind ADDR     server role: address to listen on. Client role: local source
                    address for the ping and the WebSocket (pins them to the cable).
    --peer ADDR     the other laptop's address.
    --soak SECONDS  client role: after the normal check, send 10 ping frames per
                    second for this long and print loss, RTT p50/p95/max and the
                    longest gap between replies.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
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
SOAK_INTERVAL_S = 0.1
SOAK_REPLY_TIMEOUT_S = 1.0
SOAK_MAX_GAP_S = 0.5  # the fleet link watchdog is 3 s; no gap above 500 ms is the target


def ping_host(host: str, timeout_s: float = 3.0, source: str | None = None) -> bool:
    """Send one ICMP echo request with the platform's ping command."""
    if platform.system() == "Windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000))]
        if source:
            cmd += ["-S", source]
    elif platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-t", str(max(1, int(timeout_s)))]
        if source:
            cmd += ["-S", source]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s)))]
        if source:
            cmd += ["-I", source]
    cmd.append(host)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout_s + 5, check=False)
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


def _connect_kwargs(source: str | None) -> dict:
    return {"local_addr": (source, 0)} if source else {}


async def ws_round_trips(
    url: str, frames: int = PING_FRAMES, timeout_s: float = 5.0, source: str | None = None
) -> list[float]:
    """Send ping frames and return the round-trip time of each reply in milliseconds."""
    import websockets

    rtts: list[float] = []
    async with websockets.connect(url, open_timeout=timeout_s, **_connect_kwargs(source)) as ws:
        for _ in range(frames):
            t1 = time.monotonic()
            await ws.send(json.dumps({"type": "ping", "t1": t1}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout_s))
            t4 = time.monotonic()
            if reply.get("type") != "pong":
                raise ValueError(f"unexpected reply: {reply!r}")
            rtts.append(((t4 - t1) - (reply["t3"] - reply["t2"])) * 1000.0)
    return rtts


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty list."""
    rank = max(1, int(-(-fraction * len(sorted_values) // 1)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def soak_summary(samples: list[tuple[float, float | None]]) -> dict:
    """Summarise soak samples given as (send time in s, rtt in ms or None if lost).

    max_gap_s is the longest time between two consecutive replies, counted from
    the first send to the first reply and from the last reply to the last send,
    so a link that dies and never recovers still shows a large gap.
    """
    sent = len(samples)
    rtts = sorted(r for _, r in samples if r is not None)
    lost = sent - len(rtts)
    summary: dict = {
        "sent": sent,
        "received": len(rtts),
        "lost": lost,
        "loss_pct": (100.0 * lost / sent) if sent else 0.0,
        "rtt_p50_ms": None,
        "rtt_p95_ms": None,
        "rtt_max_ms": None,
        "max_gap_s": 0.0,
    }
    if rtts:
        summary["rtt_p50_ms"] = _percentile(rtts, 0.50)
        summary["rtt_p95_ms"] = _percentile(rtts, 0.95)
        summary["rtt_max_ms"] = rtts[-1]
    if sent:
        marks = [t + r / 1000.0 for t, r in samples if r is not None]
        edges = [samples[0][0], *marks, samples[-1][0]]
        summary["max_gap_s"] = max(b - a for a, b in itertools.pairwise(edges))
    return summary


def format_soak(summary: dict) -> tuple[bool, str]:
    """Return (passed, one-line text). PASS needs zero loss and no gap above 500 ms."""
    ok = summary["received"] > 0 and summary["lost"] == 0 and summary["max_gap_s"] <= SOAK_MAX_GAP_S
    if summary["received"]:
        rtt = (f"rtt p50 {summary['rtt_p50_ms']:.2f} ms, p95 {summary['rtt_p95_ms']:.2f} ms, "
               f"max {summary['rtt_max_ms']:.2f} ms")
    else:
        rtt = "no replies"
    text = (f"soak: {'PASS' if ok else 'FAIL'} ({summary['sent']} sent, {summary['lost']} lost, "
            f"loss {summary['loss_pct']:.1f}%, {rtt}, max gap {summary['max_gap_s'] * 1000:.0f} ms)")
    return ok, text


async def _close_quietly(ws) -> None:
    if ws is not None:
        with contextlib.suppress(Exception):
            await ws.close()


async def ws_soak(
    url: str,
    seconds: float,
    interval_s: float = SOAK_INTERVAL_S,
    reply_timeout_s: float = SOAK_REPLY_TIMEOUT_S,
    source: str | None = None,
) -> list[tuple[float, float | None]]:
    """Send a ping frame every interval for the given time, reconnecting after a drop."""
    import websockets

    samples: list[tuple[float, float | None]] = []
    ws = None
    start = time.monotonic()
    n = 0
    try:
        while True:
            due = start + n * interval_s
            if due - start >= seconds:
                break
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            n += 1
            sent_at = time.monotonic()
            rtt: float | None = None
            try:
                if ws is None:
                    ws = await websockets.connect(
                        url, open_timeout=reply_timeout_s, **_connect_kwargs(source)
                    )
                await ws.send(json.dumps({"type": "ping", "t1": sent_at}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), reply_timeout_s))
                if reply.get("type") == "pong":
                    rtt = ((time.monotonic() - sent_at) - (reply["t3"] - reply["t2"])) * 1000.0
            except Exception:  # noqa: BLE001 - any drop or timeout is a lost sample; reconnect next time
                await _close_quietly(ws)
                ws = None
            samples.append((sent_at - start, rtt))
    finally:
        await _close_quietly(ws)
    return samples


def run_client(peer: str, port: int, bind: str | None = None, soak_s: float | None = None) -> bool:
    ping_ok = ping_host(peer, source=bind)
    print(f"ping {peer}: {'PASS' if ping_ok else 'FAIL'}")
    try:
        rtts = asyncio.run(ws_round_trips(f"ws://{peer}:{port}", source=bind))
    except Exception as exc:  # noqa: BLE001 - any connection failure is a FAIL, not a crash
        print(f"websocket ws://{peer}:{port}: FAIL ({type(exc).__name__}: {exc})")
        return False
    print(f"websocket ws://{peer}:{port}: PASS ({len(rtts)} frames, median RTT {statistics.median(rtts):.2f} ms)")
    if soak_s:
        print(f"soak: {soak_s:g} s at {1 / SOAK_INTERVAL_S:g} frames per second")
        samples = asyncio.run(ws_soak(f"ws://{peer}:{port}", soak_s, source=bind))
        soak_ok, text = format_soak(soak_summary(samples))
        print(text)
        return ping_ok and soak_ok
    return ping_ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the wired fleet link (AP-mode path).")
    parser.add_argument("--role", required=True, choices=["server", "client"])
    parser.add_argument("--peer", help="peer IP address (default: the other laptop's fixed address)")
    parser.add_argument("--bind", default=None,
                        help=f"server role: address to listen on (default: {SERVER_ADDR}). "
                             "client role: local source address for ping and WebSocket "
                             "(default: let the OS choose)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--duration", type=float, default=None,
                        help="server role: stop after this many seconds (default: run until Ctrl-C)")
    parser.add_argument("--soak", type=float, default=None, metavar="SECONDS",
                        help="client role: after the check, send 10 ping frames per second "
                             "for this long and print loss, RTT p50/p95/max and the longest gap")
    args = parser.parse_args(argv)
    if args.soak is not None:
        if args.role != "client":
            parser.error("--soak is only valid with --role client")
        if args.soak <= 0:
            parser.error("--soak must be greater than 0")

    if args.role == "client":
        return 0 if run_client(args.peer or SERVER_ADDR, args.port, args.bind, args.soak) else 1

    bind = args.bind or SERVER_ADDR
    peer = args.peer or CLIENT_ADDR
    ping_ok = ping_host(peer, source=args.bind)
    print(f"ping {peer}: {'PASS' if ping_ok else 'FAIL'}")
    try:
        asyncio.run(run_server(bind, args.port, args.duration))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"websocket listener on {bind}:{args.port}: FAIL ({exc})")
        return 1
    return 0 if ping_ok else 1


if __name__ == "__main__":
    sys.exit(main())
