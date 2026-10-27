#!/usr/bin/env python3
"""Fly one ArduPilot SITL drone (or a real MAVLink quad) over a coverage lawnmower.

The script plans first and connects second: it picks the altitude and speed
with flightrisk.drone.coverage.plan_coverage, builds the GPS lanes with
flightrisk.drone.geo.lawnmower_gps, and only then connects over MAVSDK. It arms,
climbs to the planned altitude, points the gimbal, flies the lanes with
goto_location and prints the position at every waypoint. When a replay
directory is given, aerial frames stand in for the camera, and detections are
geolocated to latitude and longitude with pixel_to_ground.

Gimbal pitch uses the MAVSDK convention: negative looks down, -90 is nadir.

Examples:
    scripts/start_sitl.sh --instance 0
    python scripts/sitl_fly_coverage.py --min-px 24 --preset OPEN_QUAD_4K \\
        --origin 40.6894,-111.7310 --area 300x300
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flightrisk.drone import coverage  # noqa: E402
from flightrisk.drone.geo import GpsWaypoint, UTAH_DEMO_ORIGIN, haversine_m, lawnmower_gps  # noqa: E402
from flightrisk.drone.geolocate import CameraModel as GeoCamera  # noqa: E402
from flightrisk.drone.geolocate import pixel_to_ground  # noqa: E402

logger = logging.getLogger("sitl_fly_coverage")

# ArduPilot SITL started by sim_vehicle.py -I N listens for a ground station on
# TCP port 5760 + 10 * N.
SITL_BASE_PORT = 5760
SITL_PORT_STRIDE = 10

# Telemetry stream rate parameters that the SITL image leaves at 0 Hz.
_STREAM_PARAMS = ("SR0_RAW_SENS", "SR0_EXT_STAT", "SR0_POSITION", "SR0_EXTRA1", "SR0_EXTRA2", "SR0_EXTRA3")
_STREAM_RATE_HZ = 10
_FIX_3D = 3  # mavsdk.telemetry.FixType.FIX_3D
_SETTLE_S = 15.0
_FIX_TIMEOUT_S = 120.0
_CONNECT_TIMEOUT_S = 300.0

# The climb counts as complete at this fraction of the planned altitude.
_CLIMB_TOLERANCE = 0.95


@dataclass(frozen=True)
class FlightPlan:
    """The planned coverage sortie: altitude, speed and the lane waypoints."""

    coverage: coverage.CoveragePlan
    camera: coverage.CameraModel
    waypoints: list[GpsWaypoint]
    pitch_deg: float


def parse_origin(text: str) -> tuple[float, float]:
    """Parse 'LAT,LON' into a latitude and longitude."""
    try:
        lat_s, lon_s = text.split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"origin must look like LAT,LON, got {text!r}") from exc
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise argparse.ArgumentTypeError(f"origin out of range: {text!r}")
    return lat, lon


def parse_area(text: str) -> tuple[float, float]:
    """Parse 'WIDTHxDEPTH' in metres."""
    try:
        w_s, d_s = text.lower().split("x")
        width, depth = float(w_s), float(d_s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"area must look like 300x300, got {text!r}") from exc
    if width <= 0 or depth <= 0:
        raise argparse.ArgumentTypeError("area dimensions must be positive")
    return width, depth


def sitl_address(instance: int) -> str:
    """Return the MAVSDK address of SITL instance N started by start_sitl.sh."""
    return f"tcpout://127.0.0.1:{SITL_BASE_PORT + SITL_PORT_STRIDE * instance}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-px", type=float, default=24.0, help="Smallest person height in pixels to detect.")
    parser.add_argument("--preset", default="OPEN_QUAD_4K", help="Camera preset name from flightrisk.drone.coverage.")
    parser.add_argument("--origin", type=parse_origin, default=UTAH_DEMO_ORIGIN, help="Lane origin as LAT,LON.")
    parser.add_argument("--area", type=parse_area, default=(300.0, 300.0), help="Area as WIDTHxDEPTH in metres.")
    parser.add_argument("--heading", type=float, default=0.0, help="Lane heading in compass degrees.")
    parser.add_argument("--instance", type=int, default=0, help="SITL instance number (selects the TCP port).")
    parser.add_argument("--address", default=None, help="MAVSDK address; overrides --instance (for example serial:///dev/ttyUSB0:57600).")
    parser.add_argument("--max-lanes", type=int, default=None, help="Fly only the first N lanes (2 waypoints each).")
    parser.add_argument("--replay-dir", default=None, help="Directory of aerial frames used as the camera.")
    parser.add_argument("--detect", action="store_true", help="Run the person detector on replay frames and geolocate hits.")
    parser.add_argument("--strict-health", action="store_true",
                        help="Use the stock MavlinkController health check (real hardware) instead of the SITL GPS-fix wait.")
    parser.add_argument("--no-gimbal", action="store_true", help="Skip gimbal commands.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit without connecting.")
    parser.add_argument("--climb-timeout", type=float, default=240.0, help="Seconds allowed for the climb.")
    parser.add_argument("--leg-timeout", type=float, default=600.0, help="Seconds allowed per waypoint.")
    return parser


def find_preset(name: str) -> coverage.CameraModel:
    wanted = name.strip().lower().replace(" ", "_")
    for cam in coverage.PRESETS:
        if cam.name.lower().replace(" ", "_") == wanted:
            return cam
    var = getattr(coverage, name.upper(), None)
    if isinstance(var, coverage.CameraModel):
        return var
    raise ValueError(f"Unknown preset {name!r}")


def build_plan(args: argparse.Namespace) -> FlightPlan:
    """Plan altitude, speed and lanes. Pure computation, no connection."""
    camera = find_preset(args.preset)
    if camera.is_ground:
        raise ValueError(f"Preset {camera.name!r} is a ground team and cannot fly")
    cov = coverage.plan_coverage(camera, args.min_px)
    width, depth = args.area
    lat, lon = args.origin
    waypoints = lawnmower_gps(lat, lon, width, depth, cov.lane_spacing_m, args.heading, cov.alt_m)
    if args.max_lanes is not None:
        waypoints = waypoints[: 2 * args.max_lanes]
    return FlightPlan(cov, camera, waypoints, camera.gimbal_pitch_deg)


def geo_camera(camera: coverage.CameraModel, frame_shape: tuple[int, int]) -> GeoCamera:
    """Build the geolocation camera for a frame, keeping the preset's field of view."""
    height, width = frame_shape[:2]
    return GeoCamera(width=width, height=height, hfov_deg=camera.hfov_deg)


def geolocate_detections(
    detections: Sequence[dict],
    frame_shape: tuple[int, int],
    camera: coverage.CameraModel,
    lat: float,
    lon: float,
    alt_agl_m: float,
    heading_deg: float,
    pitch_deg: float,
) -> list[tuple[float, float, float]]:
    """Return (lat, lon, confidence) for each detection whose ray reaches the ground."""
    cam = geo_camera(camera, frame_shape)
    out: list[tuple[float, float, float]] = []
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        # The feet of a standing person touch the ground, so aim at the box bottom centre.
        hit = pixel_to_ground(lat, lon, alt_agl_m, heading_deg, pitch_deg, cam, (x1 + x2) / 2.0, float(y2))
        if hit is not None:
            out.append((hit[0], hit[1], float(det.get("confidence", 0.0))))
    return out


def _make_sitl_controller_class():
    """Return a MavlinkController subclass whose connect suits the old SITL image.

    The pinned ArduCopter SITL build streams no telemetry until asked (every
    SRx_* stream rate is 0) and never reports the calibration and EKF health
    bits that MavlinkController waits for, so its stock connect would block
    forever. This variant requests the telemetry streams and waits for a 3D GPS
    fix instead. Real hardware keeps the stock health check.
    """
    import asyncio

    from flightrisk.drone import mavlink as mavlink_mod

    class SitlController(mavlink_mod.MavlinkController):
        connect_timeout_s = _CONNECT_TIMEOUT_S

        def connect(self) -> bool:
            # The stock connect allows 30 s, far less than emulated SITL needs for a GPS fix.
            with self._cmd_lock:
                self._running = True
                self._start_loop()
                try:
                    return self._run(self._connect_async(), timeout=self.connect_timeout_s)
                except Exception as exc:
                    logger.error("[%s] Connect failed: %r", self.name, exc)
                    self._running = False
                    self._stop_loop()
                    return False

        async def _connect_async(self) -> bool:
            system = mavlink_mod.System()
            await system.connect(system_address=self.host)
            async for state in system.core.connection_state():
                if state.is_connected:
                    break
            for name in _STREAM_PARAMS:
                try:
                    await system.param.set_param_int(name, _STREAM_RATE_HZ)
                except Exception as exc:  # Best effort: newer firmware names differ.
                    logger.warning("Could not set %s: %s", name, exc)

            async def wait_for_fix() -> None:
                async for gps in system.telemetry.gps_info():
                    if gps.fix_type.value >= _FIX_3D:
                        return

            try:
                await asyncio.wait_for(wait_for_fix(), _FIX_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.warning("No 3D GPS fix reported within %.0f s; continuing anyway", _FIX_TIMEOUT_S)
            await asyncio.sleep(_SETTLE_S)  # Let the EKF adopt the GPS fix before arming.
            self._system = system
            self.state.is_connected = True
            self._last_telemetry_time = time.monotonic()
            loop = self._loop
            self._telemetry_tasks = [
                loop.create_task(self._subscribe_position(system)),
                loop.create_task(self._subscribe_heading(system)),
                loop.create_task(self._subscribe_battery(system)),
                loop.create_task(self._subscribe_flight_mode(system)),
                loop.create_task(self._subscribe_gps_info(system)),
                loop.create_task(self._monitor_heartbeat()),
            ]
            return True

    return SitlController


def _set_flight_limits(controller, alt_m: float, speed_mps: float) -> None:
    """Set the takeoff altitude and the cruise speed on the autopilot."""
    system = controller._system  # The controller exposes no takeoff altitude or speed setter.
    controller._run(system.action.set_takeoff_altitude(float(alt_m)))
    controller._run(system.action.set_current_speed(float(speed_mps)))


def _wait_for(condition: Callable[[], bool], timeout: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return condition()


def fly(
    controller,
    plan: FlightPlan,
    *,
    use_gimbal: bool = True,
    detector=None,
    climb_timeout: float = 240.0,
    leg_timeout: float = 600.0,
    out: Callable[[str], None] = print,
) -> list[tuple[float, float, float]]:
    """Fly the plan with a connected controller and return the geolocated detections."""
    alt = plan.coverage.alt_m
    _wait_for(lambda: controller.state.altitude_msl is not None, 30.0)
    ground_amsl = controller.state.altitude_msl or 0.0
    out(f"Planned altitude {alt:.1f} m AGL, speed {plan.coverage.speed_mps:.1f} m/s, "
        f"{len(plan.waypoints)} waypoints, ground {ground_amsl:.1f} m AMSL")

    _set_flight_limits(controller, alt, plan.coverage.speed_mps)
    controller.takeoff()
    if not _wait_for(lambda: (controller.state.altitude_msl or ground_amsl) - ground_amsl >= alt * _CLIMB_TOLERANCE,
                     climb_timeout):
        raise TimeoutError(f"Did not reach {alt:.0f} m within {climb_timeout:.0f} s")
    out(f"At altitude: {controller.state.altitude_msl - ground_amsl:.1f} m AGL")

    if use_gimbal:
        try:
            controller.set_gimbal_pitch(plan.pitch_deg)
            out(f"Gimbal pitch commanded to {plan.pitch_deg:.0f} deg")
        except Exception as exc:  # Gimbal support in SITL is best effort.
            out(f"Gimbal command failed (continuing): {exc}")

    found: list[tuple[float, float, float]] = []
    for i, wp in enumerate(plan.waypoints):
        controller.goto_gps(wp.lat, wp.lon, ground_amsl + wp.alt_m, timeout=leg_timeout)
        s = controller.state
        err = haversine_m(s.latitude, s.longitude, wp.lat, wp.lon) if s.latitude is not None else float("nan")
        out(f"Waypoint {i + 1}/{len(plan.waypoints)}: target {wp.lat:.6f},{wp.lon:.6f} "
            f"actual {s.latitude:.6f},{s.longitude:.6f} error {err:.1f} m alt {(s.altitude_msl or 0) - ground_amsl:.1f} m")
        frame = controller.get_frame()
        if frame is not None:
            dets = detector.detect(frame) if detector is not None else []
            hits = geolocate_detections(
                dets, frame.shape, plan.camera, s.latitude, s.longitude, alt, float(s.heading or 0), plan.pitch_deg
            )
            for lat, lon, conf in hits:
                out(f"  detection at {lat:.6f},{lon:.6f} conf {conf:.2f}")
            found.extend(hits)
    out("Coverage complete, landing")
    controller.land()
    return found


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = build_plan(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Plan: {plan.camera.name} at {plan.coverage.alt_m:.1f} m, {plan.coverage.speed_mps:.1f} m/s, "
          f"lane spacing {plan.coverage.lane_spacing_m:.1f} m, {len(plan.waypoints)} waypoints")
    if args.dry_run:
        for i, wp in enumerate(plan.waypoints):
            print(f"  {i + 1}: {wp.lat:.6f},{wp.lon:.6f} alt {wp.alt_m:.1f}")
        return 0

    from flightrisk.drone.mavlink import MavlinkController  # Imported after planning on purpose.

    detector = None
    if args.detect:
        from flightrisk.vision.detector import PersonDetector

        detector = PersonDetector()
    address = args.address or sitl_address(args.instance)
    rtsp = f"dir://{args.replay_dir}" if args.replay_dir else None
    controller_cls = MavlinkController if args.strict_health else _make_sitl_controller_class()
    controller = controller_cls("sitl-coverage", host=address, rtsp_url=rtsp)
    print(f"Connecting to {address}")
    if not controller.connect():
        print("error: could not connect", file=sys.stderr)
        return 1
    try:
        fly(controller, plan, use_gimbal=not args.no_gimbal, detector=detector,
            climb_timeout=args.climb_timeout, leg_timeout=args.leg_timeout)
    finally:
        controller.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
