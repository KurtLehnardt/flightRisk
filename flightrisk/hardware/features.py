"""Hardware features: camera roles section (design detail section 6.2).

A mount has exactly one role for a flight, decided before takeoff. The level tolerance is read
from the parameter registry row ``sensing.level_pitch_tolerance_deg``; this module adds no tuning
value of its own.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Sequence

from flightrisk.hardware.catalogue import CameraSpec
from flightrisk.params.resolver import spec_for

ROLE_LEVEL = "level"
ROLE_NADIR = "nadir"
ROLE_OBLIQUE = "oblique"
ROLES = (ROLE_LEVEL, ROLE_NADIR, ROLE_OBLIQUE)

LEVEL_TOLERANCE_PARAM = "sensing.level_pitch_tolerance_deg"

# Fixed role boundary from design detail section 6.2 ("-60 degrees or lower"), not a flight parameter.
NADIR_MAX_PITCH_DEG = -60

MISSION_PITCH_REASON = "declare the gimbal's mission pitch"


def default_level_tolerance_deg() -> float:
    """The registry default of the level pitch tolerance, the same for every built-in profile."""
    spec = spec_for(LEVEL_TOLERANCE_PARAM)
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
    """One role per mount id. Cameras sharing a mount (RGB plus thermal) always share its role."""
    pitches = mission_pitch_deg or {}
    roles: dict = {}
    first: dict = {}
    for camera in cameras:
        role = derive_role(camera, pitches.get(camera.mount), tolerance_deg)
        if camera.mount in roles:
            seen = first[camera.mount]
            if (seen.mount_kind, seen.orientation) != (camera.mount_kind, camera.orientation):
                raise ValueError(f"camera mount `{camera.mount}` is declared as two different kinds of mount")
            if role != roles[camera.mount]:
                roles[camera.mount] = derive_role(seen, pitches.get(camera.mount), tolerance_deg)
                if not (seen.pitch_measured and camera.pitch_measured):
                    roles[camera.mount] = _both(seen, camera, pitches.get(camera.mount), tolerance_deg)
            continue
        roles[camera.mount] = role
        first[camera.mount] = camera
    return roles


def _both(a: CameraSpec, b: CameraSpec, pitch: Optional[float], tolerance_deg: Optional[float]) -> Optional[str]:
    """Role for two cameras on one gimbal whose measured flags differ: the stricter reading wins."""
    unmeasured = a if not a.pitch_measured else b
    return derive_role(unmeasured, pitch, tolerance_deg)


def role_conflict(mount: str, needed_roles: Sequence[str]) -> Optional[str]:
    """Refusal text when features switched on need more than one role of one mount, else None."""
    distinct = sorted(set(needed_roles))
    if len(distinct) < 2:
        return None
    if set(distinct) == {ROLE_LEVEL, ROLE_NADIR}:
        return (f"camera mount `{mount}` cannot look level for obstacle sensing and down for "
                "bird's-eye search at once")
    return f"camera mount `{mount}` cannot serve the roles {', '.join(distinct)} at once"
