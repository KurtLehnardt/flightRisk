"""Integration tests for MavlinkController against ArduPilot SITL.

These tests require a running SITL instance on udp://:14540.
Run with: pytest tests/integration/test_sitl.py -m sitl
Skip with: pytest -m "not sitl" (default in pytest.ini)

Start SITL: ./scripts/start_sitl.sh
"""
import time

import pytest

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.integration,
]

SITL_ADDRESS = "udp://:14540"
CONNECT_TIMEOUT = 30  # SITL can take a while to boot


@pytest.fixture
def sitl_available():
    """Skip test if SITL is not running (or mavsdk isn't installed).

    NOTE: amber.drone.mavlink is imported here, inside the fixture, rather
    than at module scope. tests/test_mavlink.py stubs out `sys.modules`
    with fake mavsdk classes before importing the module under test; a
    module-level import here (at collection time) would import the real
    module first, get cached in sys.modules, and make test_mavlink.py's
    stub a no-op -- silently breaking its OffboardError mocking.
    """
    try:
        from amber.drone.mavlink import HAS_MAVSDK, MavlinkController

        if not HAS_MAVSDK:
            pytest.skip("mavsdk not installed")

        ctrl = MavlinkController("sitl-test", host=SITL_ADDRESS)
        connected = ctrl.connect()
        if not connected:
            pytest.skip("SITL not available")
        yield ctrl
        ctrl.disconnect()
    except Exception as e:
        pytest.skip(f"SITL not available: {e}")


class TestSITLConnection:
    def test_connect_to_sitl(self, sitl_available):
        ctrl = sitl_available
        assert ctrl.state.is_connected

    def test_battery_telemetry(self, sitl_available):
        ctrl = sitl_available
        time.sleep(2)  # wait for telemetry
        assert ctrl.state.battery > 0

    def test_gps_position(self, sitl_available):
        ctrl = sitl_available
        time.sleep(2)
        assert ctrl.state.latitude is not None
        assert ctrl.state.longitude is not None

    def test_capabilities(self, sitl_available):
        ctrl = sitl_available
        assert ctrl.capabilities.has_gps is True
        assert ctrl.capabilities.supports_missions is True


class TestSITLFlight:
    def test_takeoff_and_land(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        time.sleep(3)
        assert ctrl.state.is_flying
        assert ctrl.state.height > 0 or ctrl.state.altitude_msl is not None
        ctrl.land()
        time.sleep(5)

    def test_hover(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        time.sleep(3)
        ctrl.hover()
        time.sleep(2)
        ctrl.land()
        time.sleep(5)

    def test_move_forward(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        time.sleep(3)
        ctrl.move("forward", 100)  # 1 meter
        time.sleep(2)
        ctrl.land()
        time.sleep(5)

    def test_rotate(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        time.sleep(3)
        initial_heading = ctrl.state.heading
        ctrl.rotate(90)
        time.sleep(2)
        # Heading should have changed
        assert initial_heading != ctrl.state.heading
        ctrl.land()
        time.sleep(5)

    def test_goto_gps(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        time.sleep(3)
        # Move slightly from current position
        lat = ctrl.state.latitude
        lon = ctrl.state.longitude
        if lat and lon:
            ctrl.goto_gps(lat + 0.0001, lon, 10.0, timeout=60)
        ctrl.land()
        time.sleep(5)
