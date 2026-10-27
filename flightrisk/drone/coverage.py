"""Coverage planner: turns a camera model into altitude, lane spacing and search time.

``plan_coverage`` is nadir (straight-down) and idealised. Swath width is the
ground footprint of the image width, lanes overlap by a fixed fraction, and
each lane end costs a fixed turn time. Battery swaps are charged per sortie.
Adding drones divides the wall-clock time evenly, which ignores coordination
losses. It suits platforms with a downward gimbal, such as the open quad.

``SweepModel`` is the model for a forward-facing camera, such as the Tello's
fixed main camera. Its ground footprint is a trapezoid ahead of the drone, and
its effective sweep width is a calibratable parameter. ``plan_coverage`` is
unchanged; callers choose the forward model explicitly by building a
``SweepModel``.
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
    max_alt_m: float = MAX_ALT_M  # Platform flight-height ceiling (AGL), at most the regulatory one.
    max_range_m: Optional[float] = None  # Maximum distance from the operator, or None if not range-limited.

    @property
    def is_ground(self) -> bool:
        return self.sweep_width_m is not None


TELLO = CameraModel(
    name="Tello",
    width_px=2592,  # Photo mode is 2592x1936. The live video stream is TELLO_VIDEO_WIDTH_PX x TELLO_VIDEO_HEIGHT_PX.
    height_px=1936,
    hfov_deg=82.6,
    gimbal_pitch_deg=0.0,
    max_speed_mps=8.0,
    flight_time_s=13 * 60,
    source=(
        "https://www.ryzerobotics.com/tello/specs "
        "(published Max Flight Height 30 m and Max Flight Distance 100 m, checked 2026-10-27)"
    ),
    max_alt_m=30.0,  # Published Max Flight Height.
    max_range_m=100.0,  # Published Max Flight Distance.
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

# The live detector sees video frames, not 2592-px photos. The Tello stream
# size of 960x720 is UNVERIFIED (the published spec says only "HD720P30").
TELLO_VIDEO_WIDTH_PX = 960
TELLO_VIDEO_HEIGHT_PX = 720


@dataclass(frozen=True)
class SweepModel:
    """Ground footprint and sweep width of a forward-facing camera.

    The optical axis is horizontal (camera pitch 0, as on the Tello), so the
    footprint is a trapezoid on the ground ahead of the drone, symmetric about
    its heading:

    * the near edge is where the bottom of the image meets the ground,
      ``near_m = height_m / tan(VFOV / 2)``;
    * the far edge is the distance at which a person is still
      ``min_person_px`` tall, ``far_m = f_px * person_height_m / min_person_px``
      with ``f_px = (width_px / 2) / tan(HFOV / 2)``, capped at
      ``max_range_m`` when one is given (for example the walls of a room);
    * the half-width at distance ``d`` is ``d * tan(HFOV / 2)``.

    The effective sweep width is ``2 * far_m * tan(HFOV / 2) * efficiency``.
    ``efficiency`` is the calibration factor (eta) and
    ``calibrated_sweep_width_m``, when set, replaces the geometric estimate
    with a measured one. Both are meant to come from the hardware rung L1
    calibration, where a person stand-in is walked across the view at measured
    distances. Until then the geometric value is an upper bound, because the
    HERIDAL ``min_person_px`` was measured on nadir imagery from 40-65 m and
    its transfer to an oblique view from about 1-2 m is UNVERIFIED.

    ``hfov_deg`` is treated as the horizontal field of view and the vertical
    one is derived from the image aspect ratio. Whether the published Tello
    82.6 degrees is horizontal or diagonal is an UNVERIFIED hardware question.
    Set ``fov_is_diagonal=True`` to read ``hfov_deg`` as the diagonal field of
    view instead: the horizontal half-angle tangent then shrinks by
    ``width / hypot(width, height)`` (0.8 for 4:3). For the Tello at 1 m that
    moves the near edge from about 1.5 m to about 1.9 m and, when the range is
    capped (for example ``max_range_m=4`` for a room), makes the sweep about
    1.25 times narrower. Uncapped, the far edge's width does not change,
    because the focal length grows by the same factor as the half-angle
    shrinks. Plans made with the horizontal reading would over-claim coverage
    if the figure turns out to be diagonal. Rung L1 settles it.
    """

    height_m: float
    width_px: int
    height_px: int
    hfov_deg: float
    min_person_px: float
    person_height_m: float = 1.7
    efficiency: float = 1.0
    max_range_m: float | None = None
    calibrated_sweep_width_m: float | None = None
    fov_is_diagonal: bool = False

    def __post_init__(self) -> None:
        if not (math.isfinite(self.height_m) and self.height_m > 0):
            raise ValueError(f"height_m must be positive, got {self.height_m!r}")
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("width_px and height_px must be positive")
        if not 0 < self.hfov_deg < 180:
            raise ValueError(f"hfov_deg must be within (0, 180), got {self.hfov_deg!r}")
        if not (math.isfinite(self.min_person_px) and self.min_person_px > 0):
            raise ValueError(f"min_person_px must be positive, got {self.min_person_px!r}")
        if not (math.isfinite(self.person_height_m) and self.person_height_m > 0):
            raise ValueError(f"person_height_m must be positive, got {self.person_height_m!r}")
        if not (math.isfinite(self.efficiency) and 0 < self.efficiency <= 1):
            raise ValueError(f"efficiency must be within (0, 1], got {self.efficiency!r}")
        if self.max_range_m is not None and not (math.isfinite(self.max_range_m) and self.max_range_m > 0):
            raise ValueError(f"max_range_m must be positive or None, got {self.max_range_m!r}")
        if self.calibrated_sweep_width_m is not None and not (
            math.isfinite(self.calibrated_sweep_width_m) and self.calibrated_sweep_width_m > 0
        ):
            raise ValueError(
                f"calibrated_sweep_width_m must be positive or None, got {self.calibrated_sweep_width_m!r}"
            )

    @classmethod
    def for_camera(
        cls,
        camera: CameraModel,
        height_m: float,
        min_person_px: float,
        *,
        width_px: int | None = None,
        height_px: int | None = None,
        **kwargs,
    ) -> SweepModel:
        """Build a sweep model from a camera preset, optionally with the video frame size.

        Only forward-facing cameras (gimbal pitch 0) are accepted: a nadir or
        tilted camera does not have this footprint, so it is refused instead
        of being modelled as forward-facing. Use ``plan_coverage`` for nadir
        platforms.
        """
        if camera.is_ground:
            raise ValueError(f"{camera.name} is a ground team and has no camera footprint")
        if camera.gimbal_pitch_deg != 0:
            raise ValueError(
                f"{camera.name} has a gimbal pitch of {camera.gimbal_pitch_deg:g} degrees; SweepModel models only "
                "a forward-facing camera (pitch 0). Use plan_coverage for a nadir camera."
            )
        return cls(
            height_m=height_m,
            width_px=width_px if width_px is not None else camera.width_px,
            height_px=height_px if height_px is not None else camera.height_px,
            hfov_deg=camera.hfov_deg,
            min_person_px=min_person_px,
            **kwargs,
        )

    @property
    def half_tan(self) -> float:
        """tan(HFOV / 2): lateral half-width per metre of distance ahead."""
        half = math.tan(math.radians(self.hfov_deg) / 2)
        if self.fov_is_diagonal:
            half *= self.width_px / math.hypot(self.width_px, self.height_px)
        return half

    @property
    def vfov_deg(self) -> float:
        return math.degrees(2 * math.atan(self.half_tan * self.height_px / self.width_px))

    @property
    def focal_px(self) -> float:
        return (self.width_px / 2) / self.half_tan

    @property
    def near_m(self) -> float:
        """Distance ahead at which the bottom edge of the image meets the ground."""
        return self.height_m / math.tan(math.radians(self.vfov_deg) / 2)

    @property
    def detection_range_m(self) -> float:
        """Distance at which a standing person is exactly ``min_person_px`` tall."""
        return self.focal_px * self.person_height_m / self.min_person_px

    @property
    def far_m(self) -> float:
        far = self.detection_range_m
        if self.max_range_m is not None:
            far = min(far, self.max_range_m)
        return far

    @property
    def usable(self) -> bool:
        """False when nothing on the ground is both in view and detectable."""
        return self.far_m > self.near_m

    @property
    def geometric_sweep_width_m(self) -> float:
        """Width of the footprint's far edge, scaled by ``efficiency``. Zero when unusable."""
        if not self.usable:
            return 0.0
        return 2 * self.far_m * self.half_tan * self.efficiency

    @property
    def sweep_width_m(self) -> float:
        """Effective sweep width: the calibrated value when set, else the geometric one."""
        if self.calibrated_sweep_width_m is not None:
            return self.calibrated_sweep_width_m
        return self.geometric_sweep_width_m

    def lane_spacing_cm(self, overlap: float = 0.0) -> int:
        """Lane spacing in whole centimetres for a given fractional overlap between lanes."""
        if not 0 <= overlap < 1:
            raise ValueError("overlap must be in [0, 1)")
        # The small epsilon keeps 2.4 m * 0.75 from flooring to 179 cm.
        spacing = math.floor(self.sweep_width_m * (1 - overlap) * 100 + 1e-9)
        if spacing <= 0:
            raise ValueError("the sweep model has no usable footprint at this height and threshold")
        return spacing


def tello_sweep_model(height_m: float = 1.0, min_person_px: float = 32.0, **kwargs) -> SweepModel:
    """Forward sweep model for the Tello main camera on its video stream.

    The default ``min_person_px`` is the conservative HERIDAL threshold from
    ``docs/altitude-benchmark.md``; whether it holds for a forward view from
    about 1 m is UNVERIFIED, so pass a calibrated ``efficiency`` or
    ``calibrated_sweep_width_m`` once rung L1 has measured one.
    """
    return SweepModel.for_camera(
        TELLO,
        height_m,
        min_person_px,
        width_px=TELLO_VIDEO_WIDTH_PX,
        height_px=TELLO_VIDEO_HEIGHT_PX,
        **kwargs,
    )


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
    feasible: bool = True
    infeasible_reason: str = ""


def required_range_m(area_m2: float) -> float:
    """Distance from a launch point at the centre of a square area to its corner."""
    return math.sqrt(area_m2) * math.sqrt(2) / 2


def plan_coverage(
    camera: CameraModel,
    min_person_px: float,
    person_height_m: float = 1.7,
    overlap: float = 0.2,
    max_alt_m: float = MAX_ALT_M,
    turn_time_s: float = 6.0,
    swap_time_s: float = DEFAULT_SWAP_TIME_S,
    area_m2: float = KM2_M2,
) -> CoveragePlan:
    """Plan a coverage sortie for one drone (or one ground team) over one km squared.

    min_person_px is the smallest person height in pixels the detector handles.
    It is a parameter so the measured value from the altitude benchmark can be
    fed in. The altitude is the highest one that keeps a person at least
    min_person_px tall, capped at min(camera.max_alt_m, max_alt_m, 120 m).

    The plan is marked infeasible when the platform has a range limit and the
    search area (launch point at its centre) reaches beyond it. The time and
    altitude are still computed so the number can be compared, but must not be
    presented as achievable. area_m2 is used only for that range check; all
    times stay per km squared.
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

    cap = min(camera.max_alt_m, max_alt_m, MAX_ALT_M)
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
    feasible = True
    reason = ""
    if camera.max_range_m is not None and required_range_m(area_m2) > camera.max_range_m:
        feasible = False
        reason = f"area needs {required_range_m(area_m2):.0f} m of range, platform limit is {camera.max_range_m:.0f} m"
    return CoveragePlan(
        camera.name, alt, gsd, swath, lane, speed, rate, total_s, swaps, person_height_m / gsd, feasible, reason
    )


def time_to_search_table(
    area_m2: float = SQ_MILE_M2,
    presets: Sequence[CameraModel] = PRESETS,
    n_drones: Sequence[int] = (1, 2, 3, 5),
    min_person_px: float = 24.0,
    **plan_kwargs,
) -> list[dict]:
    """Return rows of preset, n, alt_m, lane_m, hours and feasibility for area_m2.

    hours is the idealised time even for an infeasible row; callers show
    feasible=False rows as infeasible instead of as a time.
    """
    rows = []
    for camera in presets:
        plan = plan_coverage(camera, min_person_px, area_m2=area_m2, **plan_kwargs)
        for n in n_drones:
            hours = plan.time_per_km2_s * (area_m2 / KM2_M2) / n / 3600
            rows.append(
                {
                    "preset": camera.name,
                    "n": n,
                    "alt_m": round(plan.alt_m, 1),
                    "lane_m": round(plan.lane_spacing_m, 1),
                    "hours": round(hours, 3),
                    "feasible": plan.feasible,
                    "reason": plan.infeasible_reason,
                }
            )
    return rows


def format_markdown_table(rows: list[dict]) -> str:
    lines = ["| preset | n | alt_m | lane_m | hours |", "|---|---|---|---|---|"]
    for r in rows:
        hours = r["hours"] if r.get("feasible", True) else f"INFEASIBLE ({r['reason']})"
        lines.append(f"| {r['preset']} | {r['n']} | {r['alt_m']} | {r['lane_m']} | {hours} |")
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
