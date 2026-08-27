"""Integration tests for MavlinkController against ArduPilot SITL.

These tests require a running SITL instance on udp://:14540.
Run with: pytest tests/integration/test_sitl.py -m sitl
Skip with: pytest -m "not sitl" (default in pytest.ini)

Start SITL: ./scripts/start_sitl.sh
"""
import sys
import time

import pytest

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.integration,
]

SITL_ADDRESS = "udp://:14540"


def _wait_for(condition, timeout=10, interval=0.5):
    """Poll until condition() is truthy or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def _import_real_mavlink_controller():
    """Clear any fake mavsdk modules and import the real MavlinkController.

    tests/test_mavlink.py injects fake `mavsdk` modules into sys.modules
    at import time so its unit tests can run without the real mavsdk
    package installed. pytest imports every test file it discovers
    during collection regardless of marker filters, so those fakes end
    up cached in sys.modules even when running `pytest -m sitl` on this
    file alone. If we didn't clear them first, importing
    amber.drone.mavlink here would silently bind to the fake mavsdk
    classes and these "integration" tests would pass against mocks
    instead of a real SITL instance.

    The cache-clear + reimport is done inside a try/finally that puts
    the previous (fake) modules back afterward. That matters when SITL
    and non-SITL tests run in the *same* pytest process without the
    default `-m "not sitl"` filter (e.g. `pytest tests/ -m ""`):
    tests/test_mavlink.py uses string-target `mock.patch`
    ("amber.drone.mavlink.System", ...), which re-resolves the module
    via sys.modules / package attributes at patch time, not just at
    collection time. Leaving the real module installed permanently
    would break those patches for the rest of the session. Restoring
    the fakes afterward keeps this fixture's real-mavsdk import fully
    contained to the moment it's needed.

    Skips the calling test if the real mavsdk package isn't available.
    """
    target_names = [
        name
        for name in sys.modules
        if name == "mavsdk" or name.startswith("mavsdk.") or name == "amber.drone.mavlink"
    ]
    saved_modules = {name: sys.modules.pop(name) for name in target_names}
    parent_pkg = sys.modules.get("amber.drone")
    saved_attr = getattr(parent_pkg, "mavlink", None) if parent_pkg else None

    try:
        try:
            import mavsdk
        except ImportError:
            pytest.skip("mavsdk package not installed")

        # A real package loaded from disk has __file__ set; the in-memory
        # fake built by tests/test_mavlink.py (types.ModuleType("mavsdk"))
        # does not. This distinguishes "real mavsdk missing" from "we
        # accidentally re-imported the fake" even though the fake also
        # defines a `System` attribute.
        if getattr(mavsdk, "__file__", None) is None:
            pytest.skip(
                "Got a fake in-memory mavsdk module (likely injected by "
                "tests/test_mavlink.py) instead of the real package. Run SITL "
                "tests in their own pytest invocation, e.g.: "
                "pytest tests/integration/test_sitl.py -m sitl"
            )

        from amber.drone.mavlink import HAS_MAVSDK, MavlinkController

        if not HAS_MAVSDK:
            pytest.skip("mavsdk not installed")

        return MavlinkController
    finally:
        for name in list(sys.modules):
            if name == "mavsdk" or name.startswith("mavsdk.") or name == "amber.drone.mavlink":
                del sys.modules[name]
        sys.modules.update(saved_modules)
        if parent_pkg is not None and saved_attr is not None:
            parent_pkg.mavlink = saved_attr


@pytest.fixture
def sitl_available():
    """Connect to a live SITL instance, or skip if unavailable."""
    MavlinkController = _import_real_mavlink_controller()

    try:
        ctrl = MavlinkController("sitl-test", host=SITL_ADDRESS)
        connected = ctrl.connect()
        if not connected:
            pytest.skip("SITL not available")
    except (ConnectionError, OSError, TimeoutError, RuntimeError) as e:
        pytest.skip(f"SITL not available: {e}")

    yield ctrl

    try:
        ctrl.disconnect()
    except Exception:
        pass  # Best-effort cleanup


def test_capabilities():
    """Capabilities are static (set in __init__), so this doesn't need a
    live SITL connection or the sitl_available fixture's connect/
    disconnect cycle -- just the real mavsdk package installed.
    """
    MavlinkController = _import_real_mavlink_controller()
    ctrl = MavlinkController("caps-test", host=SITL_ADDRESS)
    assert ctrl.capabilities.has_gps is True
    assert ctrl.capabilities.supports_missions is True


class TestSITLConnection:
    def test_connect_to_sitl(self, sitl_available):
        ctrl = sitl_available
        assert ctrl.state.is_connected

    def test_battery_telemetry(self, sitl_available):
        ctrl = sitl_available
        assert _wait_for(
            lambda: ctrl.state.battery > 0, timeout=10
        ), "Battery telemetry not received"

    def test_gps_position(self, sitl_available):
        ctrl = sitl_available
        assert _wait_for(
            lambda: ctrl.state.latitude is not None
            and ctrl.state.longitude is not None,
            timeout=10,
        ), "GPS telemetry not received"


class TestSITLFlight:
    def test_takeoff_and_land(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            assert ctrl.state.altitude_msl is not None
        finally:
            ctrl.land()
            _wait_for(lambda: not ctrl.state.is_flying, timeout=15)

    def test_hover(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            ctrl.hover()
            assert ctrl.state.is_flying is True
        finally:
            ctrl.land()
            _wait_for(lambda: not ctrl.state.is_flying, timeout=15)

    def test_move_forward(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            start_position = (ctrl.state.latitude, ctrl.state.longitude)
            ctrl.move("forward", 100)  # 1 meter
            assert _wait_for(
                lambda: (ctrl.state.latitude, ctrl.state.longitude)
                != start_position,
                timeout=10,
            ), "Position did not change after move"
        finally:
            ctrl.land()
            _wait_for(lambda: not ctrl.state.is_flying, timeout=15)

    def test_rotate(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            initial_heading = ctrl.state.heading
            ctrl.rotate(90)
            assert _wait_for(
                lambda: ctrl.state.heading != initial_heading, timeout=10
            ), "Heading did not change after rotate"
        finally:
            ctrl.land()
            _wait_for(lambda: not ctrl.state.is_flying, timeout=15)

    def test_goto_gps(self, sitl_available):
        ctrl = sitl_available
        ctrl.takeoff()
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            lat = ctrl.state.latitude
            lon = ctrl.state.longitude
            assert lat is not None and lon is not None, "GPS not available"
            ctrl.goto_gps(lat + 0.0001, lon, 10.0, timeout=60)
        finally:
            ctrl.land()
            _wait_for(lambda: not ctrl.state.is_flying, timeout=15)
