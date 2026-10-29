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

    read -rs FLIGHTRISK_EDGE_TOKEN && export FLIGHTRISK_EDGE_TOKEN   # never on the command line
    python scripts/field_fleet_ground.py --drones sitl-1,sitl-2 --area-m 48x20 --slots-m 10,15 --port 9100

It prints the plan, its coverage and each drone's launch point and edge
command, waits until every edge is ready (printing why one is not), and
then asks for a typed confirmation (``start N drones``) before anything
flies; ``--yes`` skips that only together with ``--sitl``. When the lanes
are done it lands the drones one at a time, lowest slot first.

Nothing here has flown real hardware; use it against SITL (docs/sitl-demo.md).
The token comes only from ``FLIGHTRISK_EDGE_TOKEN`` and is never printed.
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightrisk import fleet_protocol as fp  # noqa: E402
from flightrisk.drone.coordinator import FleetCoordinator  # noqa: E402
from flightrisk.drone.fleet_scale import field_scale  # noqa: E402
from flightrisk.transport import GroundTransportSync  # noqa: E402

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
    parser.add_argument("--frame-heading-deg", type=float, default=0.0,
                        help="Compass bearing of the plan's forward axis that every edge must use (default 0, north)")
    parser.add_argument("--connect-timeout-s", type=float, default=600.0)
    parser.add_argument("--flight-timeout-s", type=float, default=900.0)
    parser.add_argument("--land-timeout-s", type=float, default=180.0,
                        help="How long each drone gets to land before the next one starts down (default 180)")
    parser.add_argument("--plan-only", action="store_true", help="Print the plan and the edge settings, then exit")
    parser.add_argument("--sitl", action="store_true",
                        help="The drones are simulated (SITL); required for --yes")
    parser.add_argument("--yes", action="store_true",
                        help="Start without the typed confirmation; only together with --sitl")
    return parser


#: Launch points closer than this are refused by the docs and warned about here.
MIN_HOME_SPACING_M = 10.0


def _print_plan(scale: Any, drone_ids: list[str], heading: float) -> None:
    print(f"plan: area {scale.area_cm[1] / 100:g} m deep, lanes {scale.strip_cm / 100:g} m apart "
          f"(swath {scale.swath_cm / 100:.1f} m at the lowest slot), slots {[s / 100 for s in scale.alt_slots_cm]} m, "
          f"geofence {scale.geofence_cm / 100:g} m, forward axis {heading:g} deg")
    print(f"coverage: {scale.coverage_note} (the camera's long image axis must point across the track)")
    settings = scale.edge_settings(len(drone_ids))
    for drone_id, setting in zip(drone_ids, settings, strict=True):
        print(f"  {drone_id}: place it {setting['zone_x_m']:.1f} m across from the area's near-left corner, on the "
              f"near edge; start its edge with --drone mavlink --drone-id {drone_id} --alt-slot-m "
              f"{setting['alt_slot_m']:g} --geofence-m {setting['geofence_m']:g} --frame-heading-deg {heading:g}")
    xs = [s["zone_x_m"] for s in settings]
    close = [(a, b) for a, b in itertools.pairwise(xs) if b - a < MIN_HOME_SPACING_M]
    if close:
        print(f"WARNING: launch points closer than {MIN_HOME_SPACING_M:g} m; returns and landings converge on them "
              "(docs/open-drone-support.md)")


def _confirm(n: int, input_fn: Any) -> bool:
    phrase = f"start {n} drones"
    try:
        answer = input_fn(f'Crew clear of every aircraft? Type "{phrase}" to fly, anything else aborts: ')
    except EOFError:
        return False
    return answer.strip().lower() == phrase


def main(argv: list[str] | None = None, input_fn: Any = input) -> int:
    args = build_parser().parse_args(argv)
    if args.yes and not args.sitl:
        print("--yes is only allowed with --sitl: real aircraft need the typed confirmation", file=sys.stderr)
        return 2
    drone_ids = [d.strip() for d in args.drones.split(",") if d.strip()]
    if not drone_ids or not all(fp.is_identifier(d) for d in drone_ids):
        print("--drones must be identifiers such as sitl-1,sitl-2", file=sys.stderr)
        return 2
    try:
        scale = field_scale(len(drone_ids), area_m=args.area_m, alt_slots_m=args.slots_m, overlap=args.overlap)
    except ValueError as exc:
        print(f"plan refused: {exc}", file=sys.stderr)
        return 2
    _print_plan(scale, drone_ids, args.frame_heading_deg)
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

    def latest(drone_id: str) -> dict[str, Any]:
        with lock:
            return dict(telemetry.get(drone_id) or {})

    ground = GroundTransportSync(host=args.host, port=args.port, token=token, on_control=on_control,
                                 heartbeat_interval_s=fp.HEARTBEAT_INTERVAL_S)
    ground.start()
    links.update({drone_id: GroundLink(ground, drone_id) for drone_id in drone_ids})
    coord = FleetCoordinator(links, clock=ground.now, retask_wait_s=5.0, **scale.coordinator_kwargs())
    started_flight = False
    try:
        print(f"waiting for {', '.join(drone_ids)} on ws://{args.host}:{ground.port} ...")

        def not_ready() -> dict[str, str]:
            return {d: latest(d).get("drone_link_detail") or ("no telemetry" if not latest(d) else "not ready")
                    for d in drone_ids if not latest(d).get("drone_link_ok")}

        deadline = time.monotonic() + args.connect_timeout_s
        shown: dict[str, str] = {}
        while (waiting := not_ready()):
            if waiting != shown:
                for drone_id, detail in waiting.items():
                    print(f"  {drone_id} not ready: {detail}")
                shown = waiting
            if time.monotonic() > deadline:
                print(f"edges not ready: {waiting}", file=sys.stderr)
                return 1
            time.sleep(1.0)
        for drone_id in drone_ids:
            t = latest(drone_id)
            print(f"  {drone_id} ready: battery {t.get('battery')}%, lat {t.get('lat')}, lon {t.get('lon')}")
        if not (args.yes and args.sitl) and not _confirm(len(drone_ids), input_fn):
            print("not started")
            return 1
        coord.plan()
        coord.sync(rounds=4)
        result = coord.start(lead_s=3.0)
        if not result.ok:
            print(f"start refused: {result.refusals}", file=sys.stderr)
            return 1
        started_flight = True
        print("started")
        started = time.monotonic()

        def unfinished() -> list[str]:
            return [d for d in drone_ids
                    if not latest(d).get("lanes_total") or latest(d).get("lane_index") != latest(d).get("lanes_total")]

        while unfinished():
            if time.monotonic() - started > args.flight_timeout_s:
                print(f"lanes not finished in time: {unfinished()}", file=sys.stderr)
                return 1
            time.sleep(1.0)
        print(f"all lanes flown in {time.monotonic() - started:.0f} s ({scale.coverage_note})")
        for drone_id in drone_ids:
            t = latest(drone_id)
            print(f"  {drone_id}: x {t.get('rel_x_cm')} cm, y {t.get('rel_y_cm')} cm, height {t.get('height_cm')} cm, "
                  f"lat {t.get('lat')}, lon {t.get('lon')}")
        # Staggered landing, lowest slot first: no drone descends through a
        # lower drone's slot while that drone is still up.
        order = sorted(zip(drone_ids, scale.alt_slots_cm, strict=False), key=lambda pair: pair[1])
        for drone_id, slot_cm in order:
            print(f"landing {drone_id} (slot {slot_cm / 100:g} m)")
            links[drone_id].send(fp.make_land())
            until = time.monotonic() + args.land_timeout_s
            while latest(drone_id).get("is_flying") is not False and time.monotonic() < until:
                time.sleep(1.0)
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        # LAND ALL on every exit after a start; landing is never gated.
        try:
            if started_flight:
                coord.land_all()
                ground.emergency_land_all()
        finally:
            time.sleep(1.0)
            ground.stop()


if __name__ == "__main__":
    raise SystemExit(main())
