"""Hardware features: camera roles (design detail section 6.2) and the feature gates (section 9.2).

A mount has exactly one role for a flight, decided before takeoff. The level tolerance is read
from the parameter registry row ``sensing.level_pitch_tolerance_deg``; this module adds no tuning
value of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from flightrisk.hardware.catalogue import ADAPTER_SPECS, AdapterSpec, CameraSpec, DroneModel
from flightrisk.params.resolver import spec_for

ROLE_LEVEL = "level"
ROLE_NADIR = "nadir"
ROLE_OBLIQUE = "oblique"
ROLES = (ROLE_LEVEL, ROLE_NADIR, ROLE_OBLIQUE)

LEVEL_TOLERANCE_PARAM = "sensing.level_pitch_tolerance_deg"

# Fixed role boundary from design detail section 6.2 ("-60 degrees or lower"), not a flight parameter.
NADIR_MAX_PITCH_DEG = -60

MISSION_PITCH_REASON = "declare the gimbal's mission pitch"


def default_level_tolerance_deg(profile: Optional[str] = None) -> float:
    """The registry default of the level pitch tolerance for ``profile``.

    Without a profile, or for a profile with no default, it is the first profile default in the row.
    """
    spec = spec_for(LEVEL_TOLERANCE_PARAM)
    if profile is not None and spec.defaults.get(profile) is not None:
        return float(spec.defaults[profile])
    values = [v for v in spec.defaults.values() if v is not None]
    return float(values[0])


def derive_role(camera: CameraSpec, mission_pitch_deg: Optional[float] = None,
                tolerance_deg: Optional[float] = None) -> Optional[str]:
    """Role of the mount that carries ``camera``, or None when a gimbal has no declared mission pitch."""
    if camera.mount_kind == "fixed":
        return ROLE_NADIR if camera.orientation == "downward" else ROLE_LEVEL
    if mission_pitch_deg is None:
        return None
    tolerance = default_level_tolerance_deg() if tolerance_deg is None else tolerance_deg
    if camera.pitch_measured and abs(mission_pitch_deg) <= tolerance:
        return ROLE_LEVEL
    if mission_pitch_deg <= NADIR_MAX_PITCH_DEG:
        return ROLE_NADIR
    return ROLE_OBLIQUE


def role_unavailable_reason(camera: CameraSpec, mission_pitch_deg: Optional[float] = None) -> Optional[str]:
    """Why role-gated features are unavailable on this mount, or None when it has a role."""
    if camera.mount_kind == "gimbal" and mission_pitch_deg is None:
        return MISSION_PITCH_REASON
    return None


def mount_roles(cameras: Iterable[CameraSpec], mission_pitch_deg: Mapping[str, float] = None,
                tolerance_deg: Optional[float] = None) -> dict:
    """One role per mount id. Cameras sharing a mount (RGB plus thermal) always share its role.

    A mount's role is derived once: if any camera on it reports an unmeasured pitch, the stricter
    unmeasured reading wins (ROLE-3), whatever order the cameras are listed in.
    """
    pitches = mission_pitch_deg or {}
    groups: dict = {}
    for camera in cameras:
        group = groups.setdefault(camera.mount, [])
        if group and (group[0].mount_kind, group[0].orientation) != (camera.mount_kind, camera.orientation):
            raise ValueError(f"camera mount `{camera.mount}` is declared as two different kinds of mount")
        group.append(camera)
    roles: dict = {}
    for mount, group in groups.items():
        strict = next((c for c in group if not c.pitch_measured), group[0])
        roles[mount] = derive_role(strict, pitches.get(mount), tolerance_deg)
    return roles


def role_conflict(mount: str, needed_roles: Sequence[str], level_purpose: str = "obstacle sensing") -> Optional[str]:
    """Refusal text when features switched on need more than one role of one mount, else None."""
    distinct = sorted(set(needed_roles))
    if len(distinct) < 2:
        return None
    if set(distinct) == {ROLE_LEVEL, ROLE_NADIR}:
        return (f"camera mount `{mount}` cannot look level for {level_purpose} and down for "
                "bird's-eye search at once")
    return f"camera mount `{mount}` cannot serve the roles {', '.join(distinct)} at once"


# ---------------------------------------------------------------------------------------------
# Features (design detail section 9.2)
# ---------------------------------------------------------------------------------------------

FORWARD_OBSTACLE_SENSING = "forward_obstacle_sensing"
ONBOARD_AVOIDANCE = "onboard_avoidance"
LEVEL_SEARCH = "level_search"
BIRDSEYE_SEARCH = "birdseye_search"
THERMAL_DETECTION = "thermal_detection"
APPEARANCE_MATCH = "appearance_match"
CLAUDE_SECOND_OPINION = "claude_second_opinion"

CAPTURE_SOURCES_WITH_FRAMES = ("camera", "url")
SLOT_ADAPTER_REASON = "no adapter for that protocol is installed"
SEARCH_ROLES = (ROLE_LEVEL, ROLE_NADIR)
SRC_DEFAULT = "default"


@dataclass(frozen=True)
class Feature:
    """One feature: what it needs, what happens without it, and whether it starts on by default."""

    id: str
    static_requirement: str
    runtime_requirement: Optional[str]
    when_unavailable: str
    default_on: bool = False
    safety_switch: bool = False


FEATURES: Mapping[str, Feature] = {f.id: f for f in (
    Feature(
        FORWARD_OBSTACLE_SENSING,
        "a mount in the level role with an RGB sensor, and a frame path: drone_video with the backend's "
        "STEPPED_SENSING (Tello), or capture_source with --source camera or url (MAVLink)",
        "gimbal pitch measured and within tolerance at every gate",
        "refuse the takeoff when sensing.mode is required and onboard_avoidance is not declared; "
        "refuse the start when switched on but statically unavailable",
        default_on=True, safety_switch=True),
    Feature(
        ONBOARD_AVOIDANCE,
        "the backend may declare ONBOARD_AVOIDANCE",
        "ONBOARD_AVOIDANCE declared and avoidance_status() empty at takeoff",
        "refuse the takeoff when it is the only protection and is missing"),
    Feature(
        LEVEL_SEARCH,
        "a mount in the level role",
        None,
        "refuse Plan for a unit that has neither level_search nor birdseye_search",
        default_on=True),
    Feature(
        BIRDSEYE_SEARCH,
        "a mount in the nadir role",
        None,
        "refuse the start and Plan"),
    Feature(
        THERMAL_DETECTION,
        "a camera with sensor thermal on a mount whose role the unit's search uses",
        "frames arriving from that camera",
        "refuse the start when switched on; profiles that need it refuse with unavailable"),
    Feature(
        APPEARANCE_MATCH,
        "any RGB camera",
        None,
        "degrade with a banner: detections are not scored for appearance (no RGB camera)",
        default_on=True),
    Feature(
        CLAUDE_SECOND_OPINION,
        "none on the unit; ground provider claude and cloud consent",
        "the ground's uplink check",
        "degrade with a banner: alerts are not sent to Claude, with the reason"),
)}
FEATURE_IDS = tuple(FEATURES)


class FeatureRefused(ValueError):
    """A statically unavailable feature was switched on. ``str()`` is the feature's reason."""

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(reason)
        self.feature = feature
        self.reason = reason


def _adapter_spec(model: DroneModel) -> AdapterSpec:
    """The catalogue facts of the model's adapter. An adapter missing from the catalogue has every fact off."""
    return ADAPTER_SPECS.get(model.adapter) or AdapterSpec(model.adapter, "")


def model_addable_reason(model: DroneModel) -> Optional[str]:
    """CAT-6: why a unit of this model cannot be added to a fleet (a slot adapter), else None."""
    adapter = ADAPTER_SPECS.get(model.adapter)
    if model.support == "slot" or (adapter is not None and adapter.slot):
        return SLOT_ADAPTER_REASON
    return None


def feature_reason(feature_id: str, model: DroneModel, *, mission_pitch_deg: Mapping[str, float] = None,
                   source: Optional[str] = None, stepped_sensing: Optional[bool] = None,
                   search_roles: Sequence[str] = SEARCH_ROLES,
                   tolerance_deg: Optional[float] = None) -> Optional[str]:
    """Why ``feature_id`` is statically unavailable for ``model``, or None when it is available.

    ``source`` is the edge's ``--source`` (``camera``, ``url`` ...) for a capture-source unit.
    ``stepped_sensing`` is whether the backend declares STEPPED_SENSING; it defaults to the adapter
    spec's ``stepped_sensing`` fact (False for an adapter missing from ``ADAPTER_SPECS``) and is never
    consulted for a capture-source (MAVLink) unit.
    """
    if feature_id not in FEATURES:
        raise KeyError(f"unknown feature {feature_id!r}")
    adapter = _adapter_spec(model)
    cameras = tuple(model.cameras)
    if tolerance_deg is None:
        tolerance_deg = default_level_tolerance_deg(model.profile)
    roles = mount_roles(cameras, mission_pitch_deg, tolerance_deg)
    pitch_reason = next((role_unavailable_reason(c, (mission_pitch_deg or {}).get(c.mount)) for c in cameras
                         if roles.get(c.mount) is None), None)

    def mount_with(role: str, sensor: Optional[str] = None) -> bool:
        return any(roles.get(c.mount) == role and (sensor is None or c.sensor == sensor) for c in cameras)

    if feature_id == FORWARD_OBSTACLE_SENSING:
        if not mount_with(ROLE_LEVEL, "rgb"):
            return pitch_reason or "no level RGB camera for obstacle sensing"
        if model.frame_path == "capture_source":
            if source not in CAPTURE_SOURCES_WITH_FRAMES:
                return "obstacle sensing from the camera needs --source camera or url"
            return None
        stepped = adapter.stepped_sensing if stepped_sensing is None else stepped_sensing
        return None if stepped else "the drone does not support stepped sensing"
    if feature_id == ONBOARD_AVOIDANCE:
        if adapter.declares_avoidance:
            return None
        if adapter.short_name:
            return f"the {adapter.short_name} has no onboard avoidance"
        return "this drone has no onboard avoidance"
    if feature_id == LEVEL_SEARCH:
        return None if mount_with(ROLE_LEVEL) else (pitch_reason or "no camera mount looks level")
    if feature_id == BIRDSEYE_SEARCH:
        if mount_with(ROLE_NADIR):
            return None
        if pitch_reason:
            return pitch_reason
        if adapter.short_name:
            return f"the {adapter.short_name} has one fixed forward camera"
        return ("no camera looks down: needs a downward mount or a gimbal with a mission pitch of "
                f"{NADIR_MAX_PITCH_DEG} degrees or lower")
    if feature_id == THERMAL_DETECTION:
        thermal = [c for c in cameras if c.sensor == "thermal"]
        if not thermal:
            return "no thermal camera"
        if any(roles.get(c.mount) in search_roles for c in thermal):
            return None
        return pitch_reason or "the thermal camera is on a mount the unit's search does not use"
    if feature_id == APPEARANCE_MATCH:
        return None if any(c.sensor == "rgb" for c in cameras) else "no RGB camera"
    return None  # claude_second_opinion: a ground setting, nothing on the unit.


#: The mount role each role-gated feature needs when it is switched on (ROLE-2).
FEATURE_ROLES = {FORWARD_OBSTACLE_SENSING: ROLE_LEVEL, LEVEL_SEARCH: ROLE_LEVEL, BIRDSEYE_SEARCH: ROLE_NADIR}


def requested_role_conflict(model: DroneModel, wanted: Iterable[str]) -> Optional[str]:
    """ROLE-2: refusal text when the features switched on need two roles of the one mount that can serve them.

    A role is served by a fixed mount with that role, or else by a gimbal. When a role has no fixed
    mount and the model has exactly one gimbal, that gimbal must take the role; two such roles on it
    conflict, and the text names the mount.
    """
    cameras = tuple(model.cameras)
    fixed_roles = {derive_role(c) for c in cameras if c.mount_kind == "fixed"}
    gimbals = sorted({c.mount for c in cameras if c.mount_kind == "gimbal"})
    needed = sorted({FEATURE_ROLES[f] for f in wanted if f in FEATURE_ROLES} - fixed_roles)
    if len(gimbals) != 1:
        return None
    wanted = set(wanted)
    purpose = "obstacle sensing" if FORWARD_OBSTACLE_SENSING in wanted else "level search"
    return role_conflict(gimbals[0], needed, purpose)


#: Refusal when a safety switch is switched off without the operator's confirmation (FEA-6, C5).
CONFIRM_OFF_REASON = "switching {feature} off needs the operator's confirmation"


def resolve_features(model: DroneModel, requested: Mapping[str, bool] = None,
                     request_sources: Mapping[str, str] = None, *, confirmed_off: bool = False,
                     **context) -> dict:
    """The ``gates`` dictionary: ``{feature id: {"on", "ok", "why", "src"}}`` (plain dicts and scalars).

    ``requested`` states the features someone switched on or off; the others take their default.
    Switching on a statically unavailable feature, from any source, raises ``FeatureRefused`` with the
    feature's reason. An unavailable feature that was never switched on is reported off with its reason.
    Switching a safety switch off raises ``FeatureRefused`` unless ``confirmed_off`` is True, so a
    caller cannot skip the operator's confirmation. ``context`` is passed to ``feature_reason``.
    """
    requested = requested or {}
    sources = request_sources or {}
    unknown = sorted(set(requested) - set(FEATURES))
    if unknown:
        raise KeyError(f"unknown feature {unknown[0]!r}")
    for fid, on in requested.items():
        if FEATURES[fid].safety_switch and not on and not confirmed_off:
            raise FeatureRefused(fid, CONFIRM_OFF_REASON.format(feature=fid))
    conflict = requested_role_conflict(model, [fid for fid, on in requested.items() if on])
    if conflict:
        fid = next(f for f in (BIRDSEYE_SEARCH, LEVEL_SEARCH, FORWARD_OBSTACLE_SENSING) if requested.get(f))
        raise FeatureRefused(fid, conflict)
    gates: dict = {}
    for fid, feature in FEATURES.items():
        reason = feature_reason(fid, model, **context)
        ok = reason is None
        if fid in requested:
            want, src = bool(requested[fid]), sources.get(fid, "request")
        else:
            want, src = feature.default_on, SRC_DEFAULT
        if want and not ok:
            if fid in requested:
                raise FeatureRefused(fid, reason)
            want = False  # a default never forces an unavailable feature on
        gates[fid] = {"on": want, "ok": ok, "why": reason or "", "src": src}
    return gates
