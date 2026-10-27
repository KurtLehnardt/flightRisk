"""Tests for the drone abstraction: registry, telemetry snapshot, safety
policy, the simulated (``sim``) backend, the Tello telemetry adapter, and the
dashboard wiring that depends on them."""

from __future__ import annotations

import time
import types
from unittest.mock import MagicMock

import pytest

from flightrisk.config import AmberConfig
from flightrisk.dashboard.safety import DEFAULT_RTH_MIN_BATTERY_PCT, SafetyPolicy
from flightrisk.drone import registry
from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    Capability,
    CommandNotExecutedError,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
    DroneState,
    capabilities_of,
    command_result,
    require_executed,
)
from flightrisk.drone.fake import FakeDrone, FakeDroneConfig, FakeDroneError
from flightrisk.drone.snapshot import (
    METRE,
    PERCENT,
    Reading,
    TelemetrySnapshot,
    legacy_telemetry_payload,
    read_telemetry,
    snapshot_from_state,
)


def _old_frame_payload(state):
    """The frame.telemetry dict exactly as pipeline.py built it before the refactor."""
    return {
        "battery": state.battery,
        "height": state.height,
        "temperature": state.temperature,
        "flight_time": state.flight_time,
        "is_flying": state.is_flying,
    }


def _snap(battery=None, flying=True, **kw):
    now = time.time()
    return TelemetrySnapshot(
        backend="t",
        drone_id="d1",
        captured_at=now,
        is_flying=flying,
        battery_pct=None if battery is None else Reading(battery, PERCENT, "test", now),
        **kw,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_build_through_registry(self):
        built = []

        def factory(drone_id, host, options):
            built.append((drone_id, host, dict(options)))
            return FakeDrone(drone_id, host)

        registry.register(
            "unit-test-drone",
            factory,
            default_host=lambda opts: opts.get("host") or "unit://default",
            options_from=lambda settings: {"speed": settings.get("speed", 1)},
        )
        try:
            assert registry.is_registered("unit-test-drone")
            assert "unit-test-drone" in registry.names()
            spec = registry.get("unit-test-drone")
            options = spec.options({"speed": 3, "unrelated": "x"})
            assert options == {"speed": 3}
            assert spec.default_host(options) == "unit://default"
            ctrl = spec.fleet_factory(options)("drone-1", "unit://h")
            assert ctrl.name == "drone-1"
            assert built == [("drone-1", "unit://h", {"speed": 3})]
        finally:
            registry.unregister("unit-test-drone")
        assert not registry.is_registered("unit-test-drone")

    def test_duplicate_name_rejected_unless_replace(self):
        registry.register("dup-test", lambda *a: None, default_host=lambda o: "x")
        try:
            with pytest.raises(ValueError):
                registry.register("dup-test", lambda *a: None, default_host=lambda o: "x")
            registry.register("dup-test", lambda *a: None, default_host=lambda o: "y", replace=True)
            assert registry.get("dup-test").default_host({}) == "y"
        finally:
            registry.unregister("dup-test")

    def test_unknown_backend(self):
        assert not registry.is_registered("no-such-drone")
        assert not registry.is_registered(None)
        with pytest.raises(registry.UnknownBackendError):
            registry.get("no-such-drone")

    def test_mavlink_options_keep_legacy_settings(self):
        spec = registry.get("mavlink")
        opts = spec.options({"mavlink_address": "udp://:15000", "rtsp_url": "rtsp://cam"})
        assert spec.default_host(opts) == "udp://:15000"
        assert opts["rtsp_url"] == "rtsp://cam"

    def test_failed_bootstrap_is_rolled_back_and_retried(self, tmp_path, monkeypatch):
        """The bootstrap flag is set only after the backends module imported."""
        import importlib

        module = tmp_path / "half_backends.py"
        module.write_text(
            "from flightrisk.drone.registry import register\n"
            "register('half', lambda *a: None, default_host=lambda o: 'x')\n"
            "raise ImportError('backend SDK missing')\n"
        )
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.setattr(registry, "_BOOTSTRAP_MODULE", "half_backends")
        monkeypatch.setattr(registry, "_bootstrapped", False)
        monkeypatch.setattr(registry, "_specs", {})
        with pytest.raises(ImportError, match="backend SDK missing"):
            registry.names()
        assert registry._specs == {}  # nothing half-registered is left behind
        assert registry._bootstrapped is False

        module.write_text(
            "from flightrisk.drone.registry import register\n"
            "register('half', lambda *a: None, default_host=lambda o: 'x')\n"
        )
        importlib.invalidate_caches()
        assert registry.names() == ("half",)  # retried, not stuck empty
        assert registry._bootstrapped is True
        monkeypatch.delitem(__import__("sys").modules, "half_backends", raising=False)

    def test_tello_default_host_from_config(self):
        spec = registry.get("tello")
        assert spec.default_host(spec.options({"mavlink_address": "udp://:1"})) == "192.168.10.1"

    def test_sim_options_parse_strings(self):
        spec = registry.get("sim")
        ctrl = spec.create("f1", "sim://x", spec.options({"battery_pct": "15", "drop_commands": "move,rotate"}))
        ctrl.connect()
        assert ctrl.snapshot().battery_pct.value == 15.0
        assert ctrl.move("forward", 20).outcome is CommandOutcome.DROPPED_BUSY


# ---------------------------------------------------------------------------
# Capabilities and command results
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_declared_capabilities_win(self):
        caps = DroneCapabilities(features=BASIC_FLIGHT | {Capability.RETURN_TO_HOME})
        drone = types.SimpleNamespace(capabilities=caps)
        assert capabilities_of(drone) is caps
        assert caps.supports(Capability.RETURN_TO_HOME)
        assert not caps.supports(Capability.GOTO_GPS)

    def test_legacy_object_capabilities_are_inferred(self):
        legacy = types.SimpleNamespace(return_to_home=lambda: None)
        assert capabilities_of(legacy).supports(Capability.RETURN_TO_HOME)
        assert not capabilities_of(types.SimpleNamespace()).supports(Capability.RETURN_TO_HOME)

    def test_command_result_normalization(self):
        assert command_result(None, "move") == CommandResult("move")
        dropped = CommandResult("move", CommandOutcome.DROPPED_BUSY)
        assert command_result(dropped, "move") is dropped
        assert not dropped.ok
        assert command_result(True, "move").ok
        # A bare False carries no reason: it is "not executed", not "busy".
        not_executed = command_result(False, "takeoff")
        assert not_executed.outcome is CommandOutcome.NOT_EXECUTED
        assert not not_executed.ok
        assert not_executed.command == "takeoff"

    def test_command_result_truthiness_follows_ok(self):
        assert CommandResult("move")
        for outcome in (CommandOutcome.DROPPED_BUSY, CommandOutcome.REJECTED, CommandOutcome.NOT_EXECUTED):
            assert not CommandResult("move", outcome)

    def test_require_executed(self):
        assert require_executed(None, "land").ok
        assert require_executed(True, "land").ok
        with pytest.raises(CommandNotExecutedError) as excinfo:
            require_executed(CommandResult("land", CommandOutcome.DROPPED_BUSY, "busy"), "land")
        assert excinfo.value.result.outcome is CommandOutcome.DROPPED_BUSY
        assert "land not executed (dropped_busy)" in str(excinfo.value)
        with pytest.raises(CommandNotExecutedError):
            require_executed(False, "land")


# ---------------------------------------------------------------------------
# Snapshot and legacy adapter
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_reading_age(self):
        r = Reading(1.0, METRE, "x", measured_at=100.0)
        assert r.age_s(now=102.5) == 2.5
        assert r.age_s(now=99.0) == 0.0  # never negative

    def test_from_state_maps_units_and_unknown_battery(self):
        snap = snapshot_from_state(DroneState(battery=0, height=150, temperature=40, flight_time=12, is_flying=True))
        assert snap.battery_pct is None  # 0 means unknown in the legacy cache
        assert snap.height_m.value == pytest.approx(1.5)
        assert snap.height_m.unit == "m"
        assert snap.is_flying is True

    def test_read_telemetry_falls_back_to_state_for_legacy_objects(self):
        legacy = types.SimpleNamespace(name="old-1", state=DroneState(battery=55, is_connected=True))
        snap = read_telemetry(legacy)
        assert snap.drone_id == "old-1"
        assert snap.battery_pct.value == 55
        assert snap.connected is True

    def test_read_telemetry_ignores_non_snapshot_return(self):
        """A MagicMock-style drone returns junk from snapshot(); use its state."""
        drone = MagicMock()
        drone.state = DroneState(battery=85, height=120, is_flying=True)
        assert legacy_telemetry_payload(read_telemetry(drone))["battery"] == 85

    def test_read_telemetry_survives_snapshot_errors(self):
        drone = FakeDrone()
        drone.snapshot = MagicMock(side_effect=RuntimeError("boom"))
        assert isinstance(read_telemetry(drone), TelemetrySnapshot)

    def test_to_dict_is_plain_data(self):
        snap = _snap(battery=50)
        data = snap.to_dict(now=snap.captured_at + 1)
        assert data["battery_pct"]["value"] == 50
        assert data["battery_pct"]["age_s"] == pytest.approx(1.0, abs=0.01)
        assert data["height_m"] is None

    def test_legacy_payload_unknown_is_zero(self):
        payload = legacy_telemetry_payload(_snap(), include_connected=True)
        assert payload == {
            "battery": 0, "height": 0, "temperature": 0, "flight_time": 0,
            "is_flying": True, "is_connected": False,
        }


# ---------------------------------------------------------------------------
# Safety policy
# ---------------------------------------------------------------------------


class TestSafetyPolicy:
    def test_defaults_match_config(self):
        policy = SafetyPolicy.from_config(AmberConfig())
        assert (policy.battery_warn_pct, policy.battery_critical_pct) == (20, 10)
        assert policy.rth_min_battery_pct == DEFAULT_RTH_MIN_BATTERY_PCT == 40

    def test_thresholds_come_from_config(self):
        cfg = AmberConfig()
        cfg.drone.battery_warn_threshold = 30
        cfg.drone.battery_critical_threshold = 15
        policy = SafetyPolicy.from_config(cfg)
        assert policy.battery_low(_snap(battery=30))
        assert policy.battery_critical(_snap(battery=15))
        assert not policy.battery_critical(_snap(battery=16))

    @pytest.mark.parametrize("battery,low,critical", [(21, False, False), (20, True, False), (10, True, True)])
    def test_battery_levels(self, battery, low, critical):
        policy = SafetyPolicy()
        snap = _snap(battery=battery)
        assert policy.battery_low(snap) is low
        assert policy.battery_critical(snap) is critical

    def test_unknown_or_grounded_battery_is_not_monitored(self):
        policy = SafetyPolicy()
        assert not policy.battery_critical(_snap(battery=None))
        assert not policy.battery_critical(_snap(battery=0))
        assert not policy.battery_critical(_snap(battery=5, flying=False))

    def test_return_home_needs_capability_and_reserve(self):
        policy = SafetyPolicy()
        rth = DroneCapabilities(features=BASIC_FLIGHT | {Capability.RETURN_TO_HOME})
        assert policy.can_return_home(rth, 40)
        assert not policy.can_return_home(rth, 39)
        assert not policy.can_return_home(DroneCapabilities(), 100)


# ---------------------------------------------------------------------------
# Simulated backend (sim)
# ---------------------------------------------------------------------------


class TestFakeDrone:
    def test_deterministic_latency_and_clock(self):
        slept = []
        clock = iter(range(1000, 2000))
        drone = FakeDrone(config=FakeDroneConfig(latency_s=0.25), clock=lambda: next(clock), sleep=slept.append)
        drone.connect()
        drone.takeoff()
        assert slept == [0.25, 0.25]
        assert drone.snapshot().height_m.measured_at == 1001

    def test_configured_failure_raises(self):
        drone = FakeDrone(config=FakeDroneConfig(fail_commands=frozenset({"land"})))
        drone.connect()
        with pytest.raises(FakeDroneError):
            drone.land()

    def test_connect_failure(self):
        assert FakeDrone(config=FakeDroneConfig(connect_ok=False)).connect() is False

    def test_missing_sensor_is_none(self):
        drone = FakeDrone(config=FakeDroneConfig(temperature_c=None, speed_mps=None))
        drone.connect()
        snap = drone.snapshot()
        assert snap.temperature_c is None
        assert snap.vx_mps is None and snap.ground_speed_mps is None
        assert drone.state.temperature == 0  # legacy cache keeps its old default

    def test_return_to_home_unsupported_is_rejected_not_raised(self):
        drone = FakeDrone(config=FakeDroneConfig(supports_return_to_home=False))
        assert not drone.capabilities.supports(Capability.RETURN_TO_HOME)
        assert drone.return_to_home().outcome is CommandOutcome.REJECTED

    def test_set_telemetry(self):
        drone = FakeDrone()
        drone.connect()
        drone.set_telemetry(battery_pct=9.0)
        assert drone.snapshot().battery_pct.value == 9.0
        assert drone.state.battery == 9.0


# ---------------------------------------------------------------------------
# Tello telemetry adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def tello(tello_on_wire, monkeypatch):
    """A connected TelloController running the real djitellopy client over FakeTelloWire.

    The keepalive and state-poll threads are not started; tests poll with
    ``_poll_once()``. State packets carry the values below (Tello SDK units).
    """
    import flightrisk.drone.tello as tello_mod

    monkeypatch.setattr(tello_mod.TelloController, "_start_keepalive", lambda self: None)
    monkeypatch.setattr(tello_mod.TelloController, "_start_state_polling", lambda self: None)
    ctrl, wire = tello_on_wire()
    wire.height_cm = 50
    wire.tof_cm = 60
    wire.state_overrides.update({
        "bat": "80", "templ": "40", "temph": "43", "time": "12",
        "vgx": "3", "vgy": "4", "vgz": "0", "baro": "120.00", "pitch": "0", "roll": "0", "yaw": "-90",
    })
    wire.start_state()
    assert ctrl.connect() is True
    return ctrl, wire


class TestTelloTelemetry:
    def test_connect_failure_reports_the_reason(self, tello_on_wire, capsys):
        ctrl, _ = tello_on_wire()

        def refuse():
            raise RuntimeError("Did not receive a state packet from the Tello")

        ctrl.tello.connect = refuse
        assert ctrl.connect() is False
        assert "Connection failed: Did not receive a state packet from the Tello" in capsys.readouterr().out

    def test_extended_readings_in_si_units(self, tello):
        ctrl, _ = tello
        ctrl._poll_once()
        snap = ctrl.snapshot()
        assert snap.height_m.value == pytest.approx(0.5) and snap.height_m.source == "tello.h"
        assert snap.vx_mps.value == pytest.approx(0.3)
        assert snap.ground_speed_mps.value == pytest.approx(0.5)
        assert snap.tof_m.value == pytest.approx(0.6)
        assert snap.baro_altitude_m.value == pytest.approx(120.0)
        assert snap.yaw_deg.value == -90
        assert snap.is_flying_source == "commanded"

    def test_missing_extended_field_is_none_and_not_a_poll_failure(self, tello):
        ctrl, wire = tello
        wire.omit_state_fields.add("tof")
        wire.send_state()
        ctrl._poll_once()
        snap = ctrl.snapshot()
        assert snap.tof_m is None
        assert snap.height_m is not None
        assert ctrl._poll_failures == 0
        assert ctrl.state.is_connected is True

    def test_readings_keep_poll_time_so_staleness_shows(self, tello, monkeypatch):
        import flightrisk.drone.tello as tello_mod

        ctrl, _ = tello
        clock = [1000.0]
        monkeypatch.setattr(
            tello_mod, "time", types.SimpleNamespace(time=lambda: clock[0], sleep=time.sleep, monotonic=time.monotonic)
        )
        ctrl._poll_once()
        clock[0] = 1010.0  # no poll for 10 s (e.g. skipped while a move holds the lock)
        snap = ctrl.snapshot()
        assert snap.height_m.measured_at == 1000.0
        assert snap.height_m.age_s(now=clock[0]) == pytest.approx(10.0)

    def test_crash_heuristic_unchanged(self, tello):
        ctrl, wire = tello
        assert ctrl.takeoff() is True
        # Fresh state of a drone on the ground with its motors off.
        wire.height_cm = 0
        wire.tof_cm = 10
        wire.send_state()
        ctrl._poll_once()
        ctrl._poll_once()
        assert ctrl.state.is_flying is True
        ctrl._poll_once()
        assert ctrl.state.is_flying is False

    def test_three_poll_failures_drop_connection(self, tello):
        ctrl, wire = tello
        wire.omit_state_fields.add("bat")
        wire.send_state()
        for _ in range(3):
            ctrl._poll_once()
        assert ctrl.state.is_connected is False
        assert ctrl.snapshot().connected is False

    def test_dropped_and_rejected_moves_are_reported(self, tello):
        ctrl, wire = tello
        sent_before = list(wire.sent)
        with ctrl._cmd_lock:  # another flight command holds the channel
            assert ctrl.move("forward", 50).outcome is CommandOutcome.DROPPED_BUSY
            assert ctrl.rotate(90).outcome is CommandOutcome.DROPPED_BUSY
        assert ctrl.move("sideways", 50).outcome is CommandOutcome.REJECTED
        assert wire.sent == sent_before  # none of them reached the drone
        assert ctrl.move("forward", 900) is True
        assert wire.sent[-1] == "forward 500"  # clamp unchanged

    def test_landing_refusal_is_still_a_bare_false(self, tello):
        """#87's contract is unchanged: a move refused by a landing returns False."""
        ctrl, _ = tello
        ctrl._landing = True
        assert ctrl.move("forward", 50) is False
        assert ctrl.rotate(90) is False
        assert command_result(False, "move").outcome is CommandOutcome.NOT_EXECUTED

    def test_read_battery_and_height_are_stamped_before_the_first_poll(self, tello):
        """read_battery()/read_height_cm() (the lane executor's readers) stamp what they store."""
        ctrl, _ = tello
        assert ctrl.snapshot().height_m is None  # nothing has read the height yet
        before = time.time()
        assert ctrl.read_height_cm() == 50
        assert ctrl.read_battery() == 80
        snap = ctrl.snapshot()
        assert snap.height_m.value == pytest.approx(0.5) and snap.height_m.measured_at >= before
        assert snap.battery_pct.measured_at >= before
        assert legacy_telemetry_payload(snap) == _old_frame_payload(ctrl.state)

    def test_partial_poll_keeps_what_it_read(self, tello):
        """A poll that fails part-way reports the fields it read, like the old output did."""
        ctrl, wire = tello
        wire.omit_state_fields.add("templ")  # get_temperature() fails after battery and height
        wire.send_state()
        ctrl._poll_once()
        snap = ctrl.snapshot()
        assert snap.battery_pct.value == 80 and snap.height_m.value == pytest.approx(0.5)
        assert snap.temperature_c is None
        assert ctrl._poll_failures == 1
        assert legacy_telemetry_payload(snap) == _old_frame_payload(ctrl.state)


# ---------------------------------------------------------------------------
# Dashboard wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_socketio(monkeypatch):
    from flightrisk.dashboard import app as app_mod

    events = []
    monkeypatch.setattr(app_mod.socketio, "emit", lambda event, *a, **k: events.append((event, a[0] if a else None)))
    return events


class TestDashboardWiring:
    def test_search_surfaces_dropped_moves_but_still_advances(self, clean_app_state, recorded_socketio, monkeypatch):
        from flightrisk.dashboard import app as app_mod

        drone = FakeDrone("drone-1")
        drone.connect()
        drone.takeoff()
        drone.set_busy(True)
        fleet = MagicMock()
        fleet.primary = drone
        app_mod._state["fleet"] = fleet
        app_mod._state["obstacle_guard"] = None
        monkeypatch.setattr(app_mod, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time, strftime=time.strftime))

        client = app_mod.socketio.test_client(app_mod.app)
        client.emit("start_search", {"pattern": "parallel_track"})
        deadline = time.time() + 5
        while time.time() < deadline and not any(e == "search_complete" for e, _ in recorded_socketio):
            time.sleep(0.01)
        client.disconnect()

        names = [e for e, _ in recorded_socketio]
        assert "search_complete" in names
        progress = [d for e, d in recorded_socketio if e == "search_progress"]
        dropped = [d for e, d in recorded_socketio if e == "command_dropped"]
        assert [p["waypoint"] for p in progress] == [1, 2, 3]  # still advances through every waypoint
        assert [d["waypoint"] for d in dropped] == [1, 2, 3]
        assert all(d["outcome"] == "dropped_busy" for d in dropped)

    def test_manual_command_unsupported_capability_is_refused(self, clean_app_state):
        from flightrisk.dashboard import app as app_mod

        drone = MagicMock()
        drone.capabilities = DroneCapabilities(features=frozenset({Capability.TAKEOFF_LAND}))
        fleet = MagicMock()
        fleet.primary = drone
        app_mod._state["fleet"] = fleet
        app_mod._state["logger"] = None
        client = app_mod.socketio.test_client(app_mod.app)
        client.get_received()
        client.emit("drone_command", {"command": "cw"})
        received = client.get_received()
        client.disconnect()
        assert [r["name"] for r in received] == ["error"]
        drone.rotate.assert_not_called()

    def test_manual_command_dropped_is_reported(self, clean_app_state):
        from flightrisk.dashboard import app as app_mod

        drone = FakeDrone("drone-1")
        drone.connect()
        drone.set_busy(True)
        fleet = MagicMock()
        fleet.primary = drone
        app_mod._state["fleet"] = fleet
        app_mod._state["logger"] = None
        client = app_mod.socketio.test_client(app_mod.app)
        client.get_received()
        client.emit("drone_command", {"command": "forward"})
        received = client.get_received()
        client.disconnect()
        names = [r["name"] for r in received]
        assert names == ["command_dropped", "command_ack"]

    def test_sim_source_builds_fleet_via_registry(self, clean_app_state):
        from flightrisk.dashboard import app as app_mod
        from flightrisk.dashboard.state import SourceConfig

        for key in ("detector", "reid", "face", "scorer", "tracker", "reasoning", "logger",
                    "metrics", "db", "obstacle_guard", "canon"):
            app_mod._state[key] = MagicMock()
        app_mod._state["running"] = False
        app_mod._init_pipeline(SourceConfig(source="sim", drone_options={"battery_pct": "33"}))
        try:
            fleet = app_mod._state["fleet"]
            ctrl = fleet._factory("drone-1", "sim://drone")
            assert isinstance(ctrl, FakeDrone)
            ctrl.connect()
            assert ctrl.snapshot().battery_pct.value == 33.0
        finally:
            app_mod._state["auto_connect_stop"].set()

    def test_frame_loop_battery_policy_uses_config(self, clean_app_state, monkeypatch):
        from flightrisk.config import get_config
        from flightrisk.dashboard import pipeline

        monkeypatch.setattr(get_config().drone, "battery_warn_threshold", 50)
        events = []
        socketio = types.SimpleNamespace(emit=lambda e, d=None, *a, **k: events.append((e, d)))
        drone = FakeDrone()
        drone.connect()
        drone.takeoff()
        drone.set_telemetry(battery_pct=45.0)
        pipeline.app_state.battery_warned = False
        pipeline.app_state.battery_critical = False
        pipeline._apply_battery_policy(socketio, drone, drone.snapshot(), 45.0)
        assert events == [("battery_warning", {"battery": 45.0})]

    def test_frame_loop_critical_battery_lands_and_reports_land_failure(self, clean_app_state):
        from flightrisk.dashboard import pipeline

        events = []
        socketio = types.SimpleNamespace(emit=lambda e, d=None, *a, **k: events.append((e, d)))
        drone = FakeDrone(config=FakeDroneConfig(fail_commands=frozenset({"land"})))
        drone.connect()
        drone.takeoff()
        drone.set_telemetry(battery_pct=8.0)
        log = MagicMock()
        pipeline.app_state.battery_critical = False
        pipeline._apply_battery_policy(socketio, drone, drone.snapshot(), 8.0, log)
        assert events == [("battery_critical", {"battery": 8.0})]
        log.warning.assert_called_once()
        assert log.warning.call_args.kwargs["error_type"] == "FakeDroneError"

    def test_recovery_uses_capabilities_with_fake_backend(self):
        from flightrisk.dashboard.recovery import run_confirm_recovery

        drone = FakeDrone()
        drone.connect()
        drone.takeoff()
        assert run_confirm_recovery(drone) == "returned_and_landed"
        assert [c[0] for c in drone.calls[-3:]] == ["hover", "return_to_home", "land"]

        no_rth = FakeDrone(config=FakeDroneConfig(supports_return_to_home=False))
        no_rth.connect()
        no_rth.takeoff()
        assert run_confirm_recovery(no_rth) == "landed_in_place"
        assert "return_to_home" not in [c[0] for c in no_rth.calls]

    def test_recovery_treats_unexecuted_return_home_as_failure(self):
        from flightrisk.dashboard.recovery import run_confirm_recovery

        drone = FakeDrone(config=FakeDroneConfig(drop_commands=frozenset({"return_to_home"})))
        drone.connect()
        drone.takeoff()
        assert run_confirm_recovery(drone) == "failsafe_landed"
        assert drone.snapshot().is_flying is False


class TestCli:
    def test_sim_source_and_drone_opts(self, monkeypatch):
        import sys
        from unittest.mock import patch

        import flightrisk.dashboard.__main__ as dashboard_main

        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            monkeypatch.setattr(sys, "argv", ["x", "--source", "sim", "--drone-opt", "battery_pct=15",
                                              "--drone-opt", "drop_commands=move"])
            dashboard_main.main()
        cfg = mock_run.call_args[0][0]
        assert cfg.source == "sim"
        assert cfg.drone_options == {"battery_pct": "15", "drop_commands": "move"}
        assert cfg.drone_settings()["battery_pct"] == "15"

    def test_bad_drone_opt_rejected(self, monkeypatch):
        import sys
        from unittest.mock import patch

        import flightrisk.dashboard.__main__ as dashboard_main

        with patch.object(dashboard_main, "run_dashboard") as mock_run:
            monkeypatch.setattr(sys, "argv", ["x", "--drone-opt", "novalue"])
            with pytest.raises(SystemExit):
                dashboard_main.main()
            mock_run.assert_not_called()
