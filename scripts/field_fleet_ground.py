#!/usr/bin/env python3
"""Headless ground station for a field-scale fleet of ``--drone mavlink`` edges (simulation first).

The dashboard plans at the indoor Tello scale. Until it plans field fleets
(docs/open-drone-support.md, follow-ups), this script is the ground for PX4 and
ArduPilot edges: it accepts the edges on a WebSocket port, plans the area with
``flightrisk.drone.fleet_scale.field_scale`` (area and slots in metres, lane
spacing from the camera footprint), synchronises clocks, starts every drone at
one time, waits until every drone has flown its lanes, and lands them all.
Ctrl+C at any point sends LAND ALL first.

It prints the ``--alt-slot-m`` and ``--geofence-m`` each edge must be started
with, so the edges' envelopes match the plan, then waits for the edges::

    FLIGHTRISK_EDGE_TOKEN=... python scripts/field_fleet_ground.py \\
        --drones sitl-1,sitl-2 --area-m 48x20 --slots-m 10,15 --port 9100

Nothing here has flown real hardware; use it against SITL (docs/sitl-demo.md).
The token comes only from ``FLIGHTRISK_EDGE_TOKEN`` and is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightrisk import fleet_protocol as fp
from flightrisk.drone.coordinator import FleetCoordinator
from flightrisk.drone.fleet_scale import field_scale
from flightrisk.transport import GroundTransportSync

_SEND_TIMEOUT_S = 5.0
_PING_TIMEOUT_S = 2.0


class GroundLink:
    """``EdgeLink`` for one drone over ``GroundTransportSync`` (the dashboard's ``TransportEdgeLink``, minimal).

    A coordinator ``ping`` runs as one real ping/pong round
    (``sync_clock(rounds=1)``) whose edge timestamps come back as a ``pong``.
    """

    def __init__(self, transport: GroundTransportSync, drone_id: str):
        self.on_message = None
        self.drone_id = drone_id
        self._transport = transport

    def send(self, msg: dict) -> None:
        if msg.get("type") == fp.PING:
            estimate = self._transport.sync_clock(self.drone_id, rounds=1, round_timeout_s=_PING_TIMEOUT_S,
                                                  timeout=_PING_TIMEOUT_S + _SEND_TIMEOUT_S)
            if not estimate.samples:
                raise RuntimeError(f"clock sync with {self.drone_id} returned no sample")
            sample = estimate.samples[-1]
            self.deliver({"type": fp.PONG, "t1": msg["t1"], "t2": sample.t2, "t3": sample.t3})
            return
        if not self._transport.send_to_drone(self.drone_id, msg, timeout=_SEND_TIMEOUT_S):
            raise ConnectionError(f"{msg.get('type')!r} not delivered: {self.drone_id} is not connected")

    def deliver(self, msg: dict) -> None:
        callback = self.on_message
        if callback is not None:
            callback(msg)


def _pair(text: str) -> tuple[float, float]:
    try:
        width, depth = (float(v) for v in text.lower().split("x"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected WIDTHxDEPTH in metres, got {text!r}") from exc
    return width, depth


def _floats(text: str) -> tuple[float, ...]:
    try:
        return tuple(float(v) for v in text.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected comma-separated metres, got {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--drones", required=True, help="Comma-separated drone ids, in zone order")
    parser.add_argument("--area-m", type=_pair, default=(48.0, 20.0), help="Search area WIDTHxDEPTH in metres")
    parser.add_argument("--slots-m", type=_floats, default=(10.0, 15.0), help="Altitude slots in metres")
    parser.add_argument("--overlap", type=float, default=0.2, help="Lane overlap fraction (default 0.2)")
    parser.add_argument("--host", default="127.0.0.1", help="Address to listen on (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9100, help="WebSocket port (default 9100)")
    parser.add_argument("--connect-timeout-s", type=float, default=600.0)
    parser.add_argument("--flight-timeout-s", type=float, default=900.0)
    parser.add_argument("--plan-only", action="store_true", help="Print the plan and the edge settings, then exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    drone_ids = [d.strip() for d in args.drones.split(",") if d.strip()]
    if not drone_ids or not all(fp.is_identifier(d) for d in drone_ids):
        print("--drones must be identifiers such as sitl-1,sitl-2", file=sys.stderr)
        return 2
    try:
        scale = field_scale(len(drone_ids), area_m=args.area_m, alt_slots_m=args.slots_m, overlap=args.overlap)
    except ValueError as exc:
        print(f"plan refused: {exc}", file=sys.stderr)
        return 2
    print(f"plan: area {scale.area_cm[0] / 100:g} x {scale.area_cm[1] / 100:g} m, lanes {scale.strip_cm / 100:g} m "
          f"apart, slots {[s / 100 for s in scale.alt_slots_cm]} m, geofence {scale.geofence_cm / 100:g} m")
    for drone_id, setting in zip(drone_ids, scale.edge_settings(len(drone_ids)), strict=True):
        print(f"  {drone_id}: take off {setting['zone_x_m']:g} m right of zone 1's corner; start its edge with "
              f"--drone-id {drone_id} --alt-slot-m {setting['alt_slot_m']:g} --geofence-m {setting['geofence_m']:g}")
    if args.plan_only:
        return 0

    token = os.environ.get("FLIGHTRISK_EDGE_TOKEN")
    if not token:
        print("set FLIGHTRISK_EDGE_TOKEN (the same token the edges use)", file=sys.stderr)
        return 2
    links: dict[str, GroundLink] = {}
    telemetry: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()

    def on_control(msg: dict) -> None:
        drone_id = msg.get("drone_id")
        if msg.get("type") == fp.TELEMETRY and isinstance(drone_id, str):
            with lock:
                telemetry[drone_id] = msg
        link = links.get(drone_id) if isinstance(drone_id, str) else None
        if link is not None:
            link.deliver(msg)

    ground = GroundTransportSync(host=args.host, port=args.port, token=token, on_control=on_control,
                                 heartbeat_interval_s=fp.HEARTBEAT_INTERVAL_S)
    ground.start()
    links.update({drone_id: GroundLink(ground, drone_id) for drone_id in drone_ids})
    coord = FleetCoordinator(links, clock=ground.now, retask_wait_s=5.0, **scale.coordinator_kwargs())
    try:
        print(f"waiting for {', '.join(drone_ids)} on ws://{args.host}:{ground.port} ...")

        def ready() -> list[str]:
            with lock:
                return [d for d in drone_ids if not (telemetry.get(d) or {}).get("drone_link_ok")]

        deadline = time.monotonic() + args.connect_timeout_s
        while ready():
            if time.monotonic() > deadline:
                with lock:
                    details = {d: (telemetry.get(d) or {}).get("drone_link_detail", "no telemetry") for d in ready()}
                print(f"edges not ready: {details}", file=sys.stderr)
                return 1
            time.sleep(1.0)
        coord.plan()
        coord.sync(rounds=4)
        result = coord.start(lead_s=3.0)
        if not result.ok:
            print(f"start refused: {result.refusals}", file=sys.stderr)
            return 1
        print("started")
        started = time.monotonic()

        def unfinished() -> list[str]:
            with lock:
                return [d for d in drone_ids
                        if not (telemetry.get(d) or {}).get("lanes_total")
                        or telemetry[d].get("lane_index") != telemetry[d].get("lanes_total")]

        while unfinished():
            if time.monotonic() - started > args.flight_timeout_s:
                print(f"lanes not finished in time: {unfinished()}", file=sys.stderr)
                return 1
            time.sleep(1.0)
        print(f"all lanes flown in {time.monotonic() - started:.0f} s")
        for drone_id in drone_ids:
            with lock:
                t = telemetry[drone_id]
            print(f"  {drone_id}: x {t.get('rel_x_cm')} cm, y {t.get('rel_y_cm')} cm, height {t.get('height_cm')} cm, "
                  f"lat {t.get('lat')}, lon {t.get('lon')}")
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        # LAND ALL on every exit; landing is never gated.
        try:
            coord.land_all()
            ground.emergency_land_all()
        finally:
            time.sleep(1.0)
            ground.stop()


if __name__ == "__main__":
    raise SystemExit(main())
