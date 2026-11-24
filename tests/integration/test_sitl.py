"""Integration tests for MavlinkController against ArduPilot SITL.

These tests skip unless FLIGHTRISK_SITL_ADDRESS names a running SITL, for example
FLIGHTRISK_SITL_ADDRESS=tcpout://127.0.0.1:5760. Once it is set, a failed connect fails the test.
Run with: pytest tests/integration/test_sitl.py -m sitl
Skip with: pytest -m "not sitl and not slow" (default in pytest.ini)

Start SITL: ./scripts/start_sitl.sh
"""
import os
import sys
import time

import pytest

from tests.integration import sitl_support

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.integration,
]

# MAVSDK 2 and later dropped the old ``udp://:14540`` listen form; ``udpin://0.0.0.0:14540`` is its replacement.
SITL_ADDRESS = "udpin://0.0.0.0:14540"


#: Set this to a running SITL to opt in, for example tcpout://127.0.0.1:5760. It is the same
#: variable tests/integration/test_sitl_fleet.py uses.
SITL_ADDRESS_ENV = "FLIGHTRISK_SITL_ADDRESS"


def _sitl_address() -> str:
    """Return the opted-in SITL address, or skip the calling test when it is unset."""
    address = os.environ.get(SITL_ADDRESS_ENV, "").strip()
    if not address:
        pytest.skip(f"set {SITL_ADDRESS_ENV} to a running SITL, e.g. tcpout://127.0.0.1:5760 (docs/sitl-demo.md)")
    return address


def _wait_for(condition, timeout=10, interval=0.5):
    """Poll until condition() is truthy or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


#: How long a takeoff is retried while the autopilot still refuses it (ArduCopter refuses GUIDED until its EKF
#: has an absolute position, and MAVSDK reports that refusal as a FAILED or DENIED action result).
TAKEOFF_RETRY_S = 90.0
TAKEOFF_RETRY_INTERVAL_S = 2.0
CLIMB_WAIT_S = 30.0
CLIMB_MARGIN_M = 1.5
LAND_WAIT_S = 60.0
LAND_MARGIN_M = 0.5
LAND_HOLD_S = 3.0
DISARM_WAIT_S = 5.0


def _is_refused_action(exc: BaseException) -> bool:
    """True for a MAVSDK ActionError whose result is FAILED or DENIED, matched by class name.

    Matching by name keeps the fake-module guard working: the real class is never imported here.
    """
    while exc is not None:
        if type(exc).__name__ == "ActionError":
            text = str(exc).upper()
            return "FAILED" in text or "DENIED" in text
        exc = exc.__cause__ or exc.__context__
    return False


def _takeoff(ctrl):
    """Take off, retrying while the autopilot refuses, then wait for a climb of 1.5 m above the connect-time ground."""
    ground = ctrl.sitl_ground_msl
    deadline = time.monotonic() + TAKEOFF_RETRY_S
    while True:
        try:
            ctrl.takeoff()
            break
        except Exception as exc:  # noqa: BLE001 - only a refused action is retried
            if not _is_refused_action(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(TAKEOFF_RETRY_INTERVAL_S)
    assert _wait_for(
        lambda: ctrl.state.altitude_msl is not None and ctrl.state.altitude_msl >= ground + CLIMB_MARGIN_M,
        timeout=CLIMB_WAIT_S,
    ), f"Drone did not climb {CLIMB_MARGIN_M} m above the ground altitude {ground:.1f} m"


def _armed(ctrl) -> bool:
    """Read the vehicle's armed flag once from MAVSDK; the controller's state does not carry it."""

    async def first_armed(system):
        async for armed in system.telemetry.armed():
            return armed

    return bool(ctrl._run(first_armed(ctrl._system), timeout=5))


def _land(ctrl, ground):
    """Land and wait until the drone has been within 0.5 m of the ground for 3 s, then for the automatic disarm.

    ``is_flying`` goes false as soon as LAND is accepted, so the measured altitude is the landing signal.
    """
    ctrl.land()
    deadline = time.monotonic() + LAND_WAIT_S
    on_ground_since = None
    while time.monotonic() < deadline:
        altitude = ctrl.state.altitude_msl
        if altitude is not None and abs(altitude - ground) <= LAND_MARGIN_M:
            if on_ground_since is None:
                on_ground_since = time.monotonic()
            elif time.monotonic() - on_ground_since >= LAND_HOLD_S:
                break
        else:
            on_ground_since = None
        time.sleep(0.5)
    else:
        pytest.fail(f"Drone did not land within {LAND_WAIT_S:g} s (ground {ground:.1f} m, now {ctrl.state.altitude_msl})")
    _wait_for(lambda: not _armed(ctrl), timeout=DISARM_WAIT_S)


def _import_real_mavlink_controller():
    """Clear any fake mavsdk modules and import the real MavlinkController.

    tests/test_mavlink.py injects fake `mavsdk` modules into sys.modules
    at import time so its unit tests can run without the real mavsdk
    package installed. pytest imports every test file it discovers
    during collection regardless of marker filters, so those fakes end
    up cached in sys.modules even when running `pytest -m sitl` on this
    file alone. If we didn't clear them first, importing
    flightrisk.drone.mavlink here would silently bind to the fake mavsdk
    classes and these "integration" tests would pass against mocks
    instead of a real SITL instance.

    The cache-clear + reimport is done inside a try/finally that puts
    the previous (fake) modules back afterward. That matters when SITL
    and non-SITL tests run in the *same* pytest process without the
    default `-m "not sitl and not slow"` filter (e.g. `pytest tests/ -m ""`):
    tests/test_mavlink.py uses string-target `mock.patch`
    ("flightrisk.drone.mavlink.System", ...), which re-resolves the module
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
        if name == "mavsdk" or name.startswith("mavsdk.") or name == "flightrisk.drone.mavlink"
    ]
    saved_modules = {name: sys.modules.pop(name) for name in target_names}
    parent_pkg = sys.modules.get("flightrisk.drone")
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

        from flightrisk.drone.mavlink import HAS_MAVSDK, MavlinkController

        if not HAS_MAVSDK:
            pytest.skip("mavsdk not installed")

        return MavlinkController
    finally:
        for name in list(sys.modules):
            if name == "mavsdk" or name.startswith("mavsdk.") or name == "flightrisk.drone.mavlink":
                del sys.modules[name]
        sys.modules.update(saved_modules)
        if parent_pkg is not None and saved_attr is not None:
            parent_pkg.mavlink = saved_attr


@pytest.fixture(scope="module")
def sitl_available():
    """Connect once per module to the SITL named by FLIGHTRISK_SITL_ADDRESS, or skip when it is unset.

    Module scope: Cygwin SITL exits when its one client disconnects, so every test shares one connection.
    """
    MavlinkController = _import_real_mavlink_controller()

    address = _sitl_address()
    try:
        sitl_support.wait_ready(address, sitl_support.sitl_env().ready_timeout_s)
    except sitl_support.SitlNotReady as e:
        pytest.fail(f"SITL at {address} never became ready: {e}")
    ctrl = MavlinkController("sitl-test", host=address)
    # connect() is already bounded by mavlink_cmd_timeout, so no extra guard is needed here.
    # The variable is set, so a failed connect is a real failure and must not skip.
    try:
        connected = ctrl.connect()
    except (ConnectionError, OSError, TimeoutError, RuntimeError) as e:
        pytest.fail(f"could not connect to SITL at {address}: {e}")
    if not connected:
        pytest.fail(f"could not connect to SITL at {address}")
    # The ground altitude the flight tests measure climbs and landings against.
    assert _wait_for(lambda: ctrl.state.altitude_msl is not None, timeout=30), "position telemetry not received"
    ctrl.sitl_ground_msl = ctrl.state.altitude_msl

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
        ground = ctrl.sitl_ground_msl
        _takeoff(ctrl)
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            assert ctrl.state.altitude_msl is not None
        finally:
            _land(ctrl, ground)

    def test_hover(self, sitl_available):
        ctrl = sitl_available
        ground = ctrl.sitl_ground_msl
        _takeoff(ctrl)
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            ctrl.hover()
            assert ctrl.state.is_flying is True
        finally:
            _land(ctrl, ground)

    def test_move_forward(self, sitl_available):
        ctrl = sitl_available
        ground = ctrl.sitl_ground_msl
        _takeoff(ctrl)
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
            _land(ctrl, ground)

    def test_rotate(self, sitl_available):
        ctrl = sitl_available
        ground = ctrl.sitl_ground_msl
        _takeoff(ctrl)
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
            _land(ctrl, ground)

    def test_goto_gps(self, sitl_available):
        ctrl = sitl_available
        ground = ctrl.sitl_ground_msl
        _takeoff(ctrl)
        try:
            assert _wait_for(
                lambda: ctrl.state.is_flying, timeout=10
            ), "Drone did not report flying after takeoff"
            lat = ctrl.state.latitude
            lon = ctrl.state.longitude
            assert lat is not None and lon is not None, "GPS not available"
            ctrl.goto_gps(lat + 0.0001, lon, ctrl.state.altitude_msl, timeout=60)
        finally:
            _land(ctrl, ground)
