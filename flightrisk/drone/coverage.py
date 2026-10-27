"""Coverage planner: turns a camera model into altitude, lane spacing and search time.

The model is nadir (straight-down) and idealised. Swath width is the ground
footprint of the image width, lanes overlap by a fixed fraction, and each lane
end costs a fixed turn time. Battery swaps are charged per sortie. Adding
drones divides the wall-clock time evenly, which ignores coordination losses.
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from typing import Optional, Sequence

MAX_ALT_M = 120.0  # Regulatory ceiling (AGL) the planner never exceeds.
KM2_M2 = 1_000_000.0
SQ_MILE_M2 = 2_589_988  # One square mile.
DEFAULT_SWAP_TIME_S = 180.0  # ASSUMPTION: time to land, swap a battery and relaunch.


@dataclass(frozen=True)
class CameraModel:
    """A search platform: a camera on a drone, or a ground team.

    For ground teams, sweep_width_m is set and the camera fields are unused.
    Every preset must carry a source URL or be labeled ASSUMPTION.
    """

    name: str
    width_px: int
    height_px: int
    hfov_deg: float
    gimbal_pitch_deg: float
    max_speed_mps: float
    flight_time_s: float
    source: str
    sweep_width_m: Optional[float] = None

    @property
    def is_ground(self) -> bool:
        return self.sweep_width_m is not None


TELLO = CameraModel(
    name="Tello",
    width_px=2592,  # Photo mode is 2592x1936; video is 1280x720.
    height_px=1936,
    hfov_deg=82.6,
    gimbal_pitch_deg=0.0,
    max_speed_mps=8.0,
    flight_time_s=13 * 60,
    source="https://www.ryzerobotics.com/tello/specs",
)

OPEN_QUAD_4K = CameraModel(
    name="Open quad 4K",
    width_px=3840,
    height_px=2160,
    hfov_deg=93.0,
    gimbal_pitch_deg=-90.0,
    max_speed_mps=12.0,
    flight_time_s=25 * 60,
    source=(
        "SIYI A8 mini 93 deg FOV: "
        "https://www.readymaderc.com/products/details/87067-siyi-a8-mini-4k-8mp-ultra-hd-6x-digital-zoom-gimbal-camera; "
        "12 m/s speed and 25 min endurance are ASSUMPTION"
    ),
)

GROUND_TEAM = CameraModel(
    name="Ground team",
    width_px=0,
    height_px=0,
    hfov_deg=0.0,
    gimbal_pitch_deg=0.0,
    max_speed_mps=1.5 / 3.6,  # ASSUMPTION: 1.5 km/h in terrain.
    flight_time_s=0.0,
    source=(
        "Sweep width 30 m from the 17-142 m range in "
        "https://www.archive.asrc.net/ASRC-Operations/2012-08-30-Sweep-Width-POD-Effect-Ferguson.pdf; "
        "1.5 km/h searcher speed is ASSUMPTION"
    ),
    sweep_width_m=30.0,
)

PRESETS = (GROUND_TEAM, TELLO, OPEN_QUAD_4K)


@dataclass(frozen=True)
class CoveragePlan:
    camera: str
    alt_m: float
    gsd_m_per_px: float
    swath_m: float
    lane_spacing_m: float
    speed_mps: float
    area_rate_m2_per_s: float
    time_per_km2_s: float
    battery_swaps_per_km2: int
    person_px_at_alt: float


def plan_coverage(
    camera: CameraModel,
    min_person_px: float,
    person_height_m: float = 1.7,
    overlap: float = 0.2,
    max_alt_m: float = MAX_ALT_M,
    turn_time_s: float = 6.0,
    swap_time_s: float = DEFAULT_SWAP_TIME_S,
) -> CoveragePlan:
    """Plan a coverage sortie for one drone (or one ground team) over one km squared.

    min_person_px is the smallest person height in pixels the detector handles.
    It is a parameter so the measured value from the altitude benchmark can be
    fed in. The altitude is the highest one that keeps a person at least
    min_person_px tall, capped at min(max_alt_m, 120 m).
    """
    if min_person_px <= 0:
        raise ValueError("min_person_px must be positive")
    if not 0 <= overlap < 1:
        raise ValueError("overlap must be in [0, 1)")

    if camera.is_ground:
        swath = float(camera.sweep_width_m)
        lane = swath  # Sweep width already accounts for detection probability.
        speed = camera.max_speed_mps
        rate = lane * speed
        return CoveragePlan(camera.name, 0.0, 0.0, swath, lane, speed, rate, KM2_M2 / rate, 0, 0.0)

    cap = min(max_alt_m, MAX_ALT_M)
    half_tan = math.tan(math.radians(camera.hfov_deg) / 2)
    wanted_gsd = person_height_m / min_person_px
    alt = min(cap, wanted_gsd * camera.width_px / (2 * half_tan))
    swath = 2 * alt * half_tan
    gsd = swath / camera.width_px
    lane = swath * (1 - overlap)
    speed = camera.max_speed_mps
    rate = lane * speed

    track_m = KM2_M2 / lane
    n_lanes = max(1, math.ceil(math.sqrt(KM2_M2) / lane))
    air_s = track_m / speed + n_lanes * turn_time_s
    swaps = max(0, math.ceil(air_s / camera.flight_time_s) - 1)
    total_s = air_s + swaps * swap_time_s
    return CoveragePlan(camera.name, alt, gsd, swath, lane, speed, rate, total_s, swaps, person_height_m / gsd)


def time_to_search_table(
    area_m2: float = SQ_MILE_M2,
    presets: Sequence[CameraModel] = PRESETS,
    n_drones: Sequence[int] = (1, 2, 3, 5),
    min_person_px: float = 24.0,
    **plan_kwargs,
) -> list[dict]:
    """Return rows of preset, n, alt_m, lane_m and hours to search area_m2."""
    rows = []
    for camera in presets:
        plan = plan_coverage(camera, min_person_px, **plan_kwargs)
        for n in n_drones:
            hours = plan.time_per_km2_s * (area_m2 / KM2_M2) / n / 3600
            rows.append(
                {
                    "preset": camera.name,
                    "n": n,
                    "alt_m": round(plan.alt_m, 1),
                    "lane_m": round(plan.lane_spacing_m, 1),
                    "hours": round(hours, 3),
                }
            )
    return rows


def format_markdown_table(rows: list[dict]) -> str:
    lines = ["| preset | n | alt_m | lane_m | hours |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['preset']} | {r['n']} | {r['alt_m']} | {r['lane_m']} | {r['hours']} |")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Time to search one square mile, by platform and drone count.")
    parser.add_argument("--min-px", type=float, default=24.0, help="Minimum person height in pixels.")
    args = parser.parse_args(argv)
    print(format_markdown_table(time_to_search_table(min_person_px=args.min_px)))
    print()
    for camera in PRESETS:
        print(f"- {camera.name}: {camera.source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
