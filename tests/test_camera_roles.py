"""Camera roles (ROLE-1, ROLE-2, ROLE-3, ROLE-5, CAT-8)."""

from __future__ import annotations

import pytest

from flightrisk.hardware.catalogue import CameraSpec
from flightrisk.hardware.features import (
    ROLE_LEVEL, ROLE_NADIR, ROLE_OBLIQUE, MISSION_PITCH_REASON,
    default_level_tolerance_deg, derive_role, mount_roles, role_conflict, role_unavailable_reason,
)


def _cam(**kw):
    base = dict(id="c", mount="gimbal", mount_kind="gimbal", orientation=None, pitch_min_deg=-90,
                pitch_max_deg=0, pitch_measured=True, stream_w_px=1920, stream_h_px=1080,
                hfov_deg=82, fps=30)
    base.update(kw)
    return CameraSpec(**base)


def _fixed(orientation):
    return _cam(mount="front", mount_kind="fixed", orientation=orientation, pitch_min_deg=None,
                pitch_max_deg=None, pitch_measured=False)


def test_tolerance_comes_from_registry():
    assert default_level_tolerance_deg() > 0


def test_fixed_mounts():
    assert derive_role(_fixed("forward")) == ROLE_LEVEL
    assert derive_role(_fixed("downward")) == ROLE_NADIR


@pytest.mark.parametrize("pitch,role", [
    (0, ROLE_LEVEL), (-4, ROLE_LEVEL), (-6, ROLE_OBLIQUE),
    (-59, ROLE_OBLIQUE), (-60, ROLE_NADIR), (-90, ROLE_NADIR),
])
def test_gimbal_pitches(pitch, role):
    assert derive_role(_cam(), pitch) == role


def test_unmeasured_gimbal_never_level():
    assert derive_role(_cam(pitch_measured=False), 0) != ROLE_LEVEL
    assert derive_role(_cam(pitch_measured=False), 0) == ROLE_OBLIQUE


def test_gimbal_without_mission_pitch_has_no_role():
    assert derive_role(_cam(), None) is None
    assert role_unavailable_reason(_cam(), None) == MISSION_PITCH_REASON
    assert role_unavailable_reason(_cam(), -90) is None
    assert role_unavailable_reason(_fixed("forward"), None) is None


def test_site_hex_dual_sensor_shares_role():
    rgb = _cam(id="rgb")
    ir = _cam(id="ir", sensor="thermal", radiometric=True, stream_w_px=640, stream_h_px=512, hfov_deg=50)
    assert mount_roles([rgb, ir], {"gimbal": -90}) == {"gimbal": ROLE_NADIR}
    assert mount_roles([rgb, ir], {"gimbal": 0}) == {"gimbal": ROLE_LEVEL}
    assert mount_roles([rgb, ir], {}) == {"gimbal": None}


def test_dual_sensor_with_one_unmeasured_is_never_level():
    rgb = _cam(id="rgb")
    ir = _cam(id="ir", sensor="thermal", pitch_measured=False)
    assert mount_roles([rgb, ir], {"gimbal": 0}) == {"gimbal": ROLE_OBLIQUE}
    assert mount_roles([ir, rgb], {"gimbal": 0}) == {"gimbal": ROLE_OBLIQUE}


def test_role_conflict_names_mount():
    msg = role_conflict("gimbal", [ROLE_LEVEL, ROLE_NADIR])
    assert msg == ("camera mount `gimbal` cannot look level for obstacle sensing and down for "
                   "bird's-eye search at once")
    assert role_conflict("gimbal", [ROLE_LEVEL, ROLE_LEVEL]) is None


def test_mount_role_does_not_depend_on_camera_order():
    """M3: three cameras on one gimbal, one unmeasured, give the stricter role in every order."""
    a, b = _cam(id="a"), _cam(id="b", pitch_measured=False)
    c = _cam(id="c2")
    for order in ([a, b, c], [a, b], [b, a, c], [a, c, b], [c, a, b]):
        assert mount_roles(order, {"gimbal": 0}) == {"gimbal": ROLE_OBLIQUE}, order
