"""Hardware catalogue: drone models, their cameras and their capability facts.

The built-in entries follow ``docs/design/hardware-profiles/hardware-profiles-detail.md``
section 6. This module imports only the standard library at load time, so the ground,
the launcher and the tests can read it anywhere. Every published fact carries a source
URL, a repository path or the word ASSUMPTION.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover
    from flightrisk.drone.coverage import CameraModel

FACT_STATUSES = ("verified", "repository", "vendor", "unverified", "proposed")
SUPPORT_LEVELS = ("reference", "supported", "slot")
ADAPTERS = ("tello_sdk_udp", "mavlink", "dji_msdk")
FRAME_PATHS = ("drone_video", "capture_source")
SCALES = ("room", "field")
POSITIONING = ("vps_tof", "gps", "rtk")
LINKS = ("wifi_ap", "wifi_station", "radio")

_TELLO_SPECS = "https://www.ryzerobotics.com/tello/specs"
_TELLO_MANUAL = "https://www.adorama.com/col/productManuals/RYTELO.pdf"
_TELLO_EDU = "https://core-electronics.com.au/dji-tello-edu.html.md"
_PI_CAM = "flightrisk/fleet_scale.py:58-75 (Raspberry Pi Camera Module 3 preset)"

# The most conservative value of each fact, used when a model does not state it (CAP-2).
# None has no safe direction, so it never means "no limit": a consumer that needs a None fact (a sensor
# height, a command-loss delay, the moves, a top speed, a height, an endurance or a range) must refuse the
# feature that depends on it.
CONSERVATIVE_FACTS: Mapping[str, Any] = MappingProxyType({
    "positioning": "dead_reckoning",
    "uncertainty_model": "dead_reckoning_growth",
    "heading_ref": "power_on_relative",
    "sensor_height_cm": None,
    "environment": "indoor",
    "indoor_profile_measured": False,
    "home_source": "pose_origin",
    "return_method": "edge_dead_reckoned",
    "interruptible_moves": False,
    "command_loss_action": "none",
    "command_loss_delay_s": None,
    "low_battery_action": "none",
    "critical_battery_action": "none",
    "fence_action": "none",
    "failsafes_checkable_before_takeoff": False,
    "moves": None,
    "top_speed_mps": None,
    "max_height_m": None,
    "endurance_s": None,
    "max_range_m": None,
    "commands_without_ground": False,
    "mounts_and_roles": (),
    "protection": "none",
    "battery_trust": "coarse",
    "fault_visibility": "error_replies_only",
    "detection_rate_hz": None,
    "tracking": False,
})


@dataclass(frozen=True)
class CapabilityFact:
    """One capability value with where it comes from and how far it is trusted."""

    value: Any
    source: str
    status: str

    def __post_init__(self) -> None:
        if self.status not in FACT_STATUSES:
            raise ValueError(f"unknown fact status {self.status!r}")


def conservative_fact(name: str) -> CapabilityFact:
    """Return the most conservative value of a fact, marked unverified."""
    if name not in CONSERVATIVE_FACTS:
        raise KeyError(f"unknown capability fact {name!r}")
    return CapabilityFact(CONSERVATIVE_FACTS[name], "ASSUMPTION: fact missing, most conservative value", "unverified")


@dataclass(frozen=True)
class CameraSpec:
    """One camera sensor on a mount. A dual RGB and thermal sensor is two specs sharing a mount."""

    id: str
    mount: str
    mount_kind: str  # fixed or gimbal
    orientation: Optional[str]  # forward or downward, for a fixed mount
    pitch_min_deg: Optional[float]
    pitch_max_deg: Optional[float]
    pitch_measured: bool
    stream_w_px: int
    stream_h_px: int
    hfov_deg: float
    fps: int
    sensor: str = "rgb"
    radiometric: bool = False
    photo_w_px: Optional[int] = None  # Still-photo size when it differs from the stream.
    photo_h_px: Optional[int] = None


@dataclass(frozen=True)
class DroneModel:
    """A drone model: adapter, cameras, envelope facts and capability facts."""

    id: str
    name: str
    maker: str
    support: str
    scale: str
    adapter: str
    frame_path: str
    cameras: Tuple[CameraSpec, ...]
    positioning: str
    envelope: Mapping[str, CapabilityFact]
    link: str
    safety: Mapping[str, CapabilityFact]
    profile: str
    sources: Tuple[str, ...]
    facts: Mapping[str, CapabilityFact]
    extends: Optional[str] = None
    coverage_name: str = ""
    coverage_source: str = ""

    def __post_init__(self) -> None:
        for name in ("envelope", "safety", "facts"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))

    def fact(self, name: str) -> CapabilityFact:
        """Return the stated fact, or the conservative one marked unverified."""
        return self.facts.get(name) or conservative_fact(name)

    def camera(self, camera_id: str) -> CameraSpec:
        for cam in self.cameras:
            if cam.id == camera_id:
                return cam
        raise KeyError(f"model {self.id} has no camera {camera_id!r}")

    def coverage_camera(self, camera_id: str, pitch_deg: float) -> "CameraModel":
        """Return the coverage model of one camera at a gimbal pitch (0 level, -90 straight down)."""
        from flightrisk.drone.coverage import CameraModel  # Lazy: keeps this package light.

        cam = self.camera(camera_id)
        env = self.envelope
        return CameraModel(
            name=self.coverage_name or self.name,
            width_px=cam.photo_w_px or cam.stream_w_px,
            height_px=cam.photo_h_px or cam.stream_h_px,
            hfov_deg=cam.hfov_deg,
            gimbal_pitch_deg=pitch_deg,
            max_speed_mps=float(env["max_speed_mps"].value),
            flight_time_s=float(env["endurance_s"].value),
            source=self.coverage_source,
            max_alt_m=float(env["max_height_m"].value),
            max_range_m=env["max_range_m"].value,
        )


@dataclass(frozen=True)
class AdapterSpec:
    """One drone adapter, with the facts the feature gates read instead of comparing adapter ids."""

    id: str
    description: str
    slot: bool = False
    stepped_sensing: bool = False  # The backend declares STEPPED_SENSING unless the caller says otherwise.
    declares_avoidance: bool = False  # The backend may declare ONBOARD_AVOIDANCE.
    short_name: str = ""  # The drone's name in feature reasons; empty means the generic wording.


ADAPTER_SPECS = MappingProxyType({
    "tello_sdk_udp": AdapterSpec("tello_sdk_udp", "SDK text commands on UDP 8889, state on 8890, video on 11111",
                                 stepped_sensing=True, declares_avoidance=False, short_name="Tello"),
    "mavlink": AdapterSpec("mavlink", "MAVLink through MAVSDK (--drone mavlink)",
                           stepped_sensing=False, declares_avoidance=True, short_name=""),
    "dji_msdk": AdapterSpec("dji_msdk", "DJI Mobile SDK; declared slot, no implementation", slot=True),
})


def _tello_facts() -> dict:
    repo = "flightrisk/drone/coordinator.py:146-153"
    return {
        "positioning": CapabilityFact("dead_reckoning", _TELLO_MANUAL + " p.7", "vendor"),
        "uncertainty_model": CapabilityFact("dead_reckoning_growth", "ASSUMPTION: growth placeholders, UNCALIBRATED", "unverified"),
        "heading_ref": CapabilityFact("power_on_relative", "flightrisk/drone/tello.py", "repository"),
        "sensor_height_cm": CapabilityFact((30, 1000), _TELLO_MANUAL + " p.7", "vendor"),
        "environment": CapabilityFact("indoor", "flightrisk/drone/tello.py", "repository"),
        "indoor_profile_measured": CapabilityFact(True, "flightrisk/drone/tello.py", "repository"),
        "home_source": CapabilityFact("pose_origin", "flightrisk/drone/tello.py", "repository"),
        "return_method": CapabilityFact("edge_dead_reckoned", "flightrisk/drone/tello.py", "repository"),
        "interruptible_moves": CapabilityFact("land_only", "flightrisk/drone/tello.py", "repository"),
        "command_loss_action": CapabilityFact("land", repo, "repository"),
        "command_loss_delay_s": CapabilityFact(15, repo, "repository"),
        "low_battery_action": CapabilityFact("none", "ASSUMPTION: the edge decides", "unverified"),
        "critical_battery_action": CapabilityFact("land", "ASSUMPTION: the edge decides", "unverified"),
        "fence_action": CapabilityFact("none", "ASSUMPTION: the edge decides", "unverified"),
        "failsafes_checkable_before_takeoff": CapabilityFact(False, "flightrisk/drone/tello.py", "repository"),
        "moves": CapabilityFact((20, 500), "flightrisk/drone/tello_bound.py", "repository"),
        "top_speed_mps": CapabilityFact(8.0, _TELLO_SPECS, "vendor"),
        "max_height_m": CapabilityFact(30.0, _TELLO_SPECS, "vendor"),
        "endurance_s": CapabilityFact(780, _TELLO_SPECS, "vendor"),
        "max_range_m": CapabilityFact(100.0, _TELLO_SPECS, "vendor"),
        "commands_without_ground": CapabilityFact(False, "flightrisk/drone/tello.py", "repository"),
        "mounts_and_roles": CapabilityFact((("front", "level"),), "ASSUMPTION A7: one fixed forward camera", "unverified"),
        "protection": CapabilityFact("stepped_sensing", "flightrisk/drone/tello.py:568-576", "repository"),
        "battery_trust": CapabilityFact("coarse_percentage", "ASSUMPTION: coarse percentage, UNVERIFIED", "unverified"),
        "fault_visibility": CapabilityFact("error_replies_only", "flightrisk/drone/tello.py:93-113", "repository"),
        "detection_rate_hz": CapabilityFact(None, "ASSUMPTION: set by the profile", "unverified"),
        "tracking": CapabilityFact(False, "ASSUMPTION: no tracking on the Tello", "proposed"),
    }


def _generic_facts() -> dict:
    return {
        "positioning": CapabilityFact("gps", "flightrisk/drone/autopilot.py:61-62", "repository"),
        "uncertainty_model": CapabilityFact("receiver", "flightrisk/drone/autopilot.py", "repository"),
        "heading_ref": CapabilityFact("compass", "flightrisk/drone/autopilot.py", "repository"),
        "sensor_height_cm": CapabilityFact(None, "ASSUMPTION: declared per unit", "unverified"),
        "environment": CapabilityFact("field", "flightrisk/drone/mavlink_fleet.py", "repository"),
        "indoor_profile_measured": CapabilityFact(False, "ASSUMPTION: no indoor profile measured", "unverified"),
        "home_source": CapabilityFact("autopilot_home", "flightrisk/drone/mavlink_fleet.py", "repository"),
        "return_method": CapabilityFact("edge_guided", "flightrisk/drone/mavlink_fleet.py:262-266", "repository"),
        "interruptible_moves": CapabilityFact(True, "ASSUMPTION: interruptible by mode change", "unverified"),
        "command_loss_action": CapabilityFact("rtl_or_land", "flightrisk/drone/mavlink_fleet.py:262-266", "repository"),
        "command_loss_delay_s": CapabilityFact(None, "ASSUMPTION: link.loss_hold_s", "unverified"),
        "low_battery_action": CapabilityFact("autopilot", "flightrisk/drone/autopilot.py", "repository"),
        "critical_battery_action": CapabilityFact("autopilot", "flightrisk/drone/autopilot.py", "repository"),
        "fence_action": CapabilityFact("autopilot", "flightrisk/drone/autopilot.py", "repository"),
        "failsafes_checkable_before_takeoff": CapabilityFact(True, "flightrisk/drone/autopilot.py", "repository"),
        "moves": CapabilityFact(None, "ASSUMPTION: autopilot", "unverified"),
        "top_speed_mps": CapabilityFact(12.0, "ASSUMPTION: 12 m/s", "unverified"),
        "max_height_m": CapabilityFact(120.0, "flightrisk/drone/autopilot.py:222", "repository"),
        "endurance_s": CapabilityFact(1500, "ASSUMPTION: 25 min", "unverified"),
        "max_range_m": CapabilityFact(None, "ASSUMPTION: not range-limited", "unverified"),
        "commands_without_ground": CapabilityFact(True, "ASSUMPTION: position hold", "unverified"),
        "mounts_and_roles": CapabilityFact((("main", "level"),), "ASSUMPTION: default fixed forward RGB", "unverified"),
        "protection": CapabilityFact("autopilot_or_companion", "ASSUMPTION: ArduCopter path planning or a companion camera; PX4 none", "unverified"),
        "battery_trust": CapabilityFact("autopilot_estimate", "flightrisk/drone/autopilot.py", "repository"),
        "fault_visibility": CapabilityFact("status_and_failsafe_modes", "flightrisk/drone/autopilot.py", "repository"),
        "detection_rate_hz": CapabilityFact(None, "ASSUMPTION: set by the profile", "unverified"),
        "tracking": CapabilityFact(True, "ASSUMPTION: UNVERIFIED for a fixed forward camera", "unverified"),
    }


def _tello_envelope() -> dict:
    return {
        "max_speed_mps": CapabilityFact(8.0, _TELLO_SPECS, "vendor"),
        "max_height_m": CapabilityFact(30.0, _TELLO_SPECS, "vendor"),
        "max_range_m": CapabilityFact(100.0, _TELLO_SPECS, "vendor"),
        "endurance_s": CapabilityFact(780, _TELLO_SPECS, "vendor"),
    }


def _tello_safety() -> dict:
    return {
        "command_loss": CapabilityFact("lands after about 15 s without a command", "flightrisk/drone/coordinator.py:146-153", "repository"),
        "replies": CapabilityFact("takeoff answered only once hovering; late and duplicate replies possible", "flightrisk/drone/tello.py:93-113", "repository"),
        "failsafe_checked_before_takeoff": CapabilityFact("none", "flightrisk/drone/tello.py", "repository"),
    }


_TELLO_COVERAGE_SOURCE = (
    "https://www.ryzerobotics.com/tello/specs "
    "(published Max Flight Height 30 m and Max Flight Distance 100 m, checked 2026-10-27)"
)

_TELLO_FRONT = CameraSpec(
    id="front", mount="front", mount_kind="fixed", orientation="forward",
    pitch_min_deg=None, pitch_max_deg=None, pitch_measured=False,
    stream_w_px=960, stream_h_px=720, hfov_deg=82.6, fps=30,
    photo_w_px=2592, photo_h_px=1936,
)

_TELLO_SOURCES = (_TELLO_SPECS, _TELLO_MANUAL)

BUILTIN_MODELS: Mapping[str, DroneModel] = MappingProxyType({
    "tello": DroneModel(
        id="tello", name="Ryze Tello", maker="Ryze Tech / DJI", support="reference", scale="room",
        adapter="tello_sdk_udp", frame_path="drone_video", cameras=(_TELLO_FRONT,), positioning="vps_tof",
        envelope=_tello_envelope(), link="wifi_ap", safety=_tello_safety(), profile="tello",
        sources=_TELLO_SOURCES, facts=_tello_facts(),
        coverage_name="Tello", coverage_source=_TELLO_COVERAGE_SOURCE,
    ),
    "tello_edu": DroneModel(
        id="tello_edu", name="Ryze Tello EDU", maker="Ryze Tech / DJI", support="reference", scale="room",
        adapter="tello_sdk_udp", frame_path="drone_video", cameras=(_TELLO_FRONT,), positioning="vps_tof",
        envelope=_tello_envelope(), link="wifi_ap", safety=_tello_safety(), profile="tello",
        sources=_TELLO_SOURCES + (_TELLO_EDU,), facts=_tello_facts(),
        coverage_name="Tello", coverage_source=_TELLO_COVERAGE_SOURCE,
    ),
    "generic_multirotor": DroneModel(
        id="generic_multirotor", name="Generic multirotor", maker="Various", support="supported", scale="field",
        adapter="mavlink", frame_path="capture_source",
        cameras=(CameraSpec(
            id="main", mount="main", mount_kind="fixed", orientation="forward",
            pitch_min_deg=None, pitch_max_deg=None, pitch_measured=False,
            stream_w_px=1920, stream_h_px=1080, hfov_deg=66.0, fps=30,
        ),),
        positioning="gps",
        envelope={
            "max_speed_mps": CapabilityFact(12.0, "ASSUMPTION: 12 m/s", "unverified"),
            "max_height_m": CapabilityFact(120.0, "flightrisk/drone/autopilot.py:222", "repository"),
            "max_range_m": CapabilityFact(None, "ASSUMPTION: not range-limited", "unverified"),
            "endurance_s": CapabilityFact(1500, "ASSUMPTION: 25 min", "unverified"),
        },
        link="radio",
        safety={
            "command_loss": CapabilityFact("autopilot failsafe, then RTL or land after link.loss_hold_s", "flightrisk/drone/mavlink_fleet.py:262-266", "repository"),
            "failsafe_checked_before_takeoff": CapabilityFact("link, battery and geofence failsafes", "flightrisk/drone/autopilot.py", "repository"),
        },
        profile="generic_multirotor",
        sources=(_PI_CAM + ": 66 degree field of view", "ASSUMPTION: 12 m/s and 25 min endurance"),
        facts=_generic_facts(),
        coverage_name="Generic multirotor",
        coverage_source=_PI_CAM + "; 12 m/s speed and 25 min endurance are ASSUMPTION",
    ),
})


def render_catalogue_markdown() -> str:
    """Render the catalogue as the text of ``docs/hardware-catalogue.md``."""
    out = [
        "# Hardware catalogue",
        "",
        "Generated by `flightrisk.hardware.catalogue.render_catalogue_markdown()`. Do not edit by hand.",
        "",
        "## Adapters",
        "",
    ]
    for spec in ADAPTER_SPECS.values():
        out.append(f"- `{spec.id}`{' (slot)' if spec.slot else ''}: {spec.description}")
    for model in BUILTIN_MODELS.values():
        out += ["", f"## {model.name} (`{model.id}`)", ""]
        out += [
            f"- Support: {model.support}",
            f"- Scale: {model.scale}",
            f"- Adapter: `{model.adapter}`",
            f"- Frame path: `{model.frame_path}`",
            f"- Positioning: `{model.positioning}`",
            f"- Link: `{model.link}`",
            f"- Profile: `{model.profile}`",
        ]
        if model.cameras:
            out += ["", "| Camera | Mount | Kind | Stream | Field of view | Sensor |", "| --- | --- | --- | --- | --- | --- |"]
            for c in model.cameras:
                out.append(
                    f"| {c.id} | {c.mount} | {c.mount_kind} {c.orientation or ''} | "
                    f"{c.stream_w_px}x{c.stream_h_px} at {c.fps} fps | {c.hfov_deg} deg | {c.sensor} |"
                )
        for title, group in (("Envelope", model.envelope), ("Safety", model.safety), ("Facts", model.facts)):
            if group:
                out += ["", f"### {title}", "", "| Name | Value | Source | Status |", "| --- | --- | --- | --- |"]
                for key, f in group.items():
                    out.append(f"| {key} | {f.value} | {f.source} | {f.status} |")
    return "\n".join(out) + "\n"
