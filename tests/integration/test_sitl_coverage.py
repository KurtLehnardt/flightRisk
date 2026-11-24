"""Coverage flight against SITL, in its own module so the connection of test_sitl.py is gone first.

The module fixture of tests/integration/test_sitl.py is finalized, and its mavsdk_server reaped by conftest.py,
before this module starts. A second client on the same vehicle would otherwise be refused (one TCP client per
ArduCopter serial port, one UDP bind per PX4 port).

Run with: pytest tests/integration/test_sitl_coverage.py -m sitl
"""
import sys

import pytest

from tests.integration import sitl_support
from tests.integration.test_sitl import _import_real_mavlink_controller, _sitl_address

pytestmark = [
    pytest.mark.sitl,
    pytest.mark.integration,
]


class TestSITLCoverageFlight:
    """Fly two lanes of the planned lawnmower with scripts/sitl_fly_coverage.py.

    Needs SITL started with ./scripts/start_sitl.sh (instance 0, TCP port 5760).
    """

    def test_fly_two_lanes(self):
        import importlib.util
        from pathlib import Path

        _import_real_mavlink_controller()  # Skips when the real mavsdk is missing.
        address = _sitl_address()  # Skips when FLIGHTRISK_SITL_ADDRESS is unset.
        try:
            sitl_support.wait_ready(address, sitl_support.sitl_env().ready_timeout_s)
        except sitl_support.SitlNotReady as e:
            pytest.fail(f"SITL at {address} never became ready: {e}")
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("sitl_fly_coverage_it", root / "scripts" / "sitl_fly_coverage.py")
        script = importlib.util.module_from_spec(spec)
        # Python 3.13 needs the module in sys.modules while it runs (dataclasses look it up there).
        sys.modules[spec.name] = script
        try:
            spec.loader.exec_module(script)
        except BaseException:
            sys.modules.pop(spec.name, None)
            raise

        # Re-import the real controller for the script, then restore the fakes
        # that the unit tests rely on once this test is done.
        saved = {n: m for n, m in sys.modules.items() if n == "mavsdk" or n.startswith("mavsdk.") or n == "flightrisk.drone.mavlink"}
        for name in saved:
            del sys.modules[name]
        try:
            from flightrisk.drone.mavlink import MavlinkController  # noqa: F401  (real module)

            args = script.build_parser().parse_args(["--max-lanes", "2", "--area", "300x300"])
            plan = script.build_plan(args)
            assert len(plan.waypoints) == 4
            controller = script._make_sitl_controller_class()("sitl-coverage-it", host=address)
            assert controller.connect(), "SITL not reachable (run scripts/start_sitl.sh)"
            try:
                lines: list[str] = []
                script.fly(controller, plan, out=lines.append, climb_timeout=300.0, leg_timeout=300.0)
                assert sum(1 for line in lines if line.startswith("Waypoint")) == 4
            finally:
                controller.disconnect()
        finally:
            for name in [n for n in sys.modules if n == "mavsdk" or n.startswith("mavsdk.") or n == "flightrisk.drone.mavlink"]:
                del sys.modules[name]
            sys.modules.update(saved)
