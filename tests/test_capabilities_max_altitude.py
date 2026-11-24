import pytest

from flightrisk.drone.controller import DroneCapabilities
from flightrisk.drone.fake import FakeDrone
from flightrisk.drone.mavlink import MavlinkController
from flightrisk.drone.template_backend import TemplateDrone
from flightrisk.drone.tello import FakeTelloController


def test_unknown_airframe_maximum_constructs():
    assert DroneCapabilities(max_altitude_m=None).max_altitude_m is None


def test_default_is_still_120():
    assert DroneCapabilities().max_altitude_m == 120


def test_known_value_is_kept():
    assert DroneCapabilities(max_altitude_m=35).max_altitude_m == 35


@pytest.mark.parametrize(
    "build,expected",
    [
        (lambda: FakeDrone(), 10),
        (lambda: FakeTelloController(), 10),
        (lambda: TemplateDrone(name="cap"), 10),
        (lambda: MavlinkController(name="cap", host="udp://:14540"), 120),
    ],
    ids=["fake", "fake_tello", "template", "mavlink"],
)
def test_built_in_backends_declare_what_they_declared(build, expected):
    assert build().capabilities.max_altitude_m == expected
