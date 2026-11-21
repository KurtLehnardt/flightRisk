"""Tests for the obstacle sensing contract (``flightrisk.drone.obstacle_sensor``)."""
from __future__ import annotations

import threading
import time

import pytest

from flightrisk.drone import obstacle_sensor as os_
from flightrisk.drone.controller import (
    BASIC_FLIGHT,
    Capability,
    CommandOutcome,
    CommandResult,
    DroneCapabilities,
)
from flightrisk.drone.obstacle_sensor import (
    BlockedZone,
    FakeObstacleSensor,
    LatestFrame,
    NullSensor,
    ObstacleSensor,
    SenseResult,
)
from flightrisk.drone.safety import RelativePose
from tests.lane_executor_support import FakeClock


class TestEnumMembers:
    def test_new_capabilities_are_not_basic_flight(self):
        assert Capability.STEPPED_SENSING not in BASIC_FLIGHT
        assert Capability.ONBOARD_AVOIDANCE not in BASIC_FLIGHT
        assert not DroneCapabilities().supports(Capability.STEPPED_SENSING)
        caps = DroneCapabilities(features=BASIC_FLIGHT | {Capability.STEPPED_SENSING})
        assert caps.supports(Capability.STEPPED_SENSING)
        assert not caps.supports(Capability.ONBOARD_AVOIDANCE)

    def test_stopped_short_is_not_ok(self):
        result = CommandResult("move", CommandOutcome.STOPPED_SHORT, "stopped at 120 cm")
        assert not result.ok
        assert not result
        assert CommandOutcome.STOPPED_SHORT.value == "stopped_short"


class TestSenseResult:
    def test_states_and_properties(self):
        assert SenseResult.make_clear().clear
        assert SenseResult.make_blocked().blocked
        assert SenseResult.make_unavailable().unavailable

    def test_unknown_state_is_refused(self):
        with pytest.raises(ValueError):
            SenseResult("maybe")

    def test_cut_reasons(self):
        assert SenseResult.make_blocked(os_.CUE_DEPTH).cut_reason == os_.CUT_OBSTACLE
        assert SenseResult.make_blocked(os_.CUE_PERSON).cut_reason == os_.CUT_PERSON
        assert SenseResult.make_blocked(os_.CUE_AUTOPILOT).cut_reason == os_.CUT_AUTOPILOT
        assert SenseResult.make_unavailable(os_.REASON_TIMEOUT).cut_reason == os_.CUT_SENSOR_STALE
        assert set(os_.CUT_REASONS) >= os_.REFLIGHT_CUT_REASONS


class TestConstants:
    def test_design_defaults(self):
        assert os_.OBSTACLE_STEP_CM == 50
        assert os_.OBSTACLE_SETTLE_S == 0.3
        assert os_.OBSTACLE_FRAME_WAIT_S == 0.7
        assert os_.OBSTACLE_SENSE_TIMEOUT_S == 2.0
        assert os_.OBSTACLE_SENSE_RETRIES == 3
        assert os_.OBSTACLE_SENSOR_DOWN_S == 10
        assert os_.OBSTACLE_PERSON_FRACTION == 0.55
        assert os_.OBSTACLE_MIN_SAFE_DEPTH == 0.35
        assert os_.OBSTACLE_DETECTION_WAIT_S == 1.0

    def test_env_names_are_prefixed(self):
        for name in (
            os_.ENV_OBSTACLE_SENSING, os_.ENV_OBSTACLE_STEP_CM, os_.ENV_OBSTACLE_TURN,
            os_.ENV_OBSTACLE_MIN_SAFE_DEPTH, os_.ENV_OBSTACLE_PERSON_FRACTION,
            os_.ENV_FAKE_OBSTACLE_Y_CM,
        ):
            assert name.startswith("FLIGHTRISK_")

    def test_telemetry_fields_are_unique_and_prefixed(self):
        assert len(set(os_.TELEMETRY_FIELDS)) == len(os_.TELEMETRY_FIELDS) == 8
        assert all(f.startswith("obstacle_") for f in os_.TELEMETRY_FIELDS)
        # Eight cuts of two numbers fit the privacy screen's 16-number list.
        assert os_.MAX_CUTS_LISTED * 2 == 16

    def test_flight_safety_events_start_with_edge(self):
        events = [v for k, v in vars(os_).items() if k.startswith("EVENT_")]
        assert events and all(e.startswith("edge_") for e in events)


class TestNullSensor:
    def test_always_clear_and_satisfies_protocol(self):
        sensor = NullSensor()
        assert isinstance(sensor, ObstacleSensor)
        result = sensor.sense(after=0.0)
        assert result.clear and result.cue == os_.CUE_NONE
        assert sensor.status()["mode"] == os_.SENSING_OFF


class TestBlockedZone:
    def test_heading_wraps_modulo_360(self):
        zone = BlockedZone(heading_deg=0.0)
        assert zone.matches(0, 0, 0)
        assert zone.matches(0, 0, 360)
        assert zone.matches(0, 0, -0.5)
        assert not zone.matches(0, 0, 90)

    def test_bounds_are_closed_and_optional(self):
        zone = BlockedZone(heading_deg=None, y_min=250)
        assert zone.matches(5, 250, 123)
        assert not zone.matches(5, 249.9, 123)


class TestFakeObstacleSensor:
    def make(self, **kw):
        pose = RelativePose()
        return pose, FakeObstacleSensor(lambda: pose, **kw)

    def test_clear_until_the_pose_reaches_the_zone(self):
        pose, sensor = self.make(blocked=[BlockedZone(heading_deg=0, y_min=250)])
        assert isinstance(sensor, ObstacleSensor)
        assert sensor.sense(after=1.0).clear
        pose.y_cm = 250
        result = sensor.sense(after=2.0)
        assert result.blocked and result.cue == os_.CUE_DEPTH
        pose.heading_deg = 90
        assert sensor.sense(after=3.0).clear

    def test_call_log_records_after_and_pose(self):
        pose, sensor = self.make()
        pose.x_cm, pose.y_cm, pose.heading_deg = 10, 20, 90
        sensor.sense(after=12.5, timeout_s=1.5)
        (call,) = sensor.calls
        assert (call.after, call.timeout_s) == (12.5, 1.5)
        assert (call.x_cm, call.y_cm, call.heading_deg) == (10, 20, 90)
        assert call.outcome == os_.STATE_CLEAR

    def test_overrides_apply_per_call_then_fall_through(self):
        pose, sensor = self.make(blocked=[BlockedZone(heading_deg=None)])
        boom = RuntimeError("camera died")
        custom = SenseResult.make_clear(cue=os_.CUE_PERSON)
        sensor.queue("stale", "timeout", boom, custom, None)
        stale = sensor.sense(0.0)
        assert stale.unavailable and stale.reason == os_.REASON_STALE
        timeout = sensor.sense(0.0)
        assert timeout.unavailable and timeout.reason == os_.REASON_TIMEOUT
        with pytest.raises(RuntimeError, match="camera died"):
            sensor.sense(0.0)
        assert sensor.sense(0.0) is custom
        assert sensor.sense(0.0).blocked  # the None entry falls through to the zone
        assert sensor.sense(0.0).blocked  # script exhausted
        assert [c.outcome for c in sensor.calls[:4]] == ["stale", "timeout", "error", "scripted"]

    def test_unknown_override_is_refused(self):
        _, sensor = self.make()
        sensor.queue("bogus")
        with pytest.raises(ValueError):
            sensor.sense(0.0)

    def test_person_zone_gives_person_cut_reason(self):
        _, sensor = self.make(blocked=[BlockedZone(heading_deg=None, cue=os_.CUE_PERSON)])
        assert sensor.sense(0.0).cut_reason == os_.CUT_PERSON


class TestLatestFrame:
    def test_empty_holder_gives_up(self):
        holder = LatestFrame()
        assert holder.latest() is None
        assert holder.wait_for(after=time.monotonic(), wait_s=0.05) is None

    def test_records_arrival_time_and_count(self):
        clock = FakeClock(100.0)
        holder = LatestFrame(clock)
        holder("a")
        clock.advance(1.0)
        holder.update("b")
        latest = holder.latest()
        assert (latest.frame, latest.arrived_at) == ("b", 101.0)
        assert holder.frames_seen == 2

    def test_needs_a_frame_at_or_after_after_plus_settle(self):
        clock = FakeClock(100.0)
        holder = LatestFrame(clock)
        holder("old")  # arrives at 100.0
        assert holder.wait_for(after=99.8, settle_s=0.3, wait_s=0) is None  # needs 100.1
        assert holder.wait_for(after=99.7, settle_s=0.3, wait_s=0).frame == "old"  # 100.0 is enough
        clock.advance(0.2)
        holder("new")  # 100.2
        assert holder.wait_for(after=99.8, settle_s=0.3, wait_s=0).frame == "new"

    def test_waits_for_a_frame_that_arrives_in_time(self):
        holder = LatestFrame()
        after = time.monotonic()
        timer = threading.Timer(0.1, holder.update, args=("late",))
        timer.start()
        try:
            got = holder.wait_for(after=after, settle_s=0.0, wait_s=5.0)
        finally:
            timer.join()
        assert got is not None and got.frame == "late"

    def test_gives_up_when_only_an_old_frame_exists(self):
        holder = LatestFrame()
        holder("old")
        time.sleep(0.02)
        start = time.monotonic()
        assert holder.wait_for(after=start, settle_s=0.0, wait_s=0.1) is None
        assert time.monotonic() - start < 3.0


class TestLatestFrameOffer:
    """``offer`` stamps each source frame object once, whichever feed brings it (review R2-B1)."""

    def test_the_same_object_is_never_stamped_again(self):
        clock = FakeClock(100.0)
        holder = LatestFrame(clock)
        frame = object()
        assert holder.offer(frame) is True
        clock.advance(5.0)
        assert holder.offer(frame) is False
        assert holder(frame) is None and holder.frames_seen == 1
        latest = holder.latest()
        assert latest.frame is frame and latest.arrived_at == 100.0
        assert holder.wait_for(after=104.0, settle_s=0.3, wait_s=0) is None

    def test_a_new_object_is_stamped_even_when_it_equals_the_last(self):
        clock = FakeClock(100.0)
        holder = LatestFrame(clock)
        assert holder.offer([1, 2, 3]) is True
        clock.advance(0.5)
        assert holder.offer([1, 2, 3]) is True
        assert holder.latest().arrived_at == 100.5 and holder.frames_seen == 2

    def test_the_check_runs_on_the_source_before_the_copy(self):
        holder = LatestFrame(FakeClock(100.0))
        copies = []

        def copy(source):
            copies.append(source)
            return ("copy of", source)

        source = object()
        assert holder.offer(source, copy) is True
        assert holder.offer(source, copy) is False
        assert holder.offer(holder.latest().frame, copy) is True  # the stored copy is not the source
        assert copies == [source, ("copy of", source)]
        assert holder.latest().frame == ("copy of", ("copy of", source))

    def test_feeds_share_one_record(self):
        holder = LatestFrame(FakeClock(100.0))
        first, second = object(), object()
        assert holder.offer(first, lambda f: ("copy", f)) is True
        assert holder(first) is None  # another feed, no copy: still refused
        assert holder.offer(second) is True
        assert holder.offer(first) is True  # an older object offered again after another is new again
        assert holder.frames_seen == 3

    def test_a_freed_frame_never_hides_a_new_one(self):
        # The last source is held, so a new array can never reuse its id and be taken for it.
        holder = LatestFrame(FakeClock(100.0))
        fed = [holder.offer(bytearray(64), bytes) for _ in range(500)]
        assert all(fed) and holder.frames_seen == 500

    def test_concurrent_feeds_store_one_object_once(self):
        holder = LatestFrame(FakeClock(100.0))
        frame = object()
        start = threading.Barrier(8)
        results: list[bool] = []
        lock = threading.Lock()

        def feed():
            start.wait(5)
            stored = holder.offer(frame, lambda f: ("copy", f))
            with lock:
                results.append(stored)

        threads = [threading.Thread(target=feed) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert sorted(results) == [False] * 7 + [True]
        assert holder.frames_seen == 1

    def test_update_still_stores_unconditionally(self):
        clock = FakeClock(100.0)
        holder = LatestFrame(clock)
        frame = object()
        holder.update(frame)
        clock.advance(1.0)
        holder.update(frame)
        assert holder.latest().arrived_at == 101.0 and holder.frames_seen == 2
