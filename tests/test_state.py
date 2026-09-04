"""Tests for flightrisk.dashboard.state — AppState dataclass and thread safety."""

import threading
import time

import pytest

from flightrisk.dashboard.state import AppState, SourceConfig, _StateDictCompat


class TestSourceConfig:
    """Verify SourceConfig defaults and field assignment."""

    def test_defaults(self):
        cfg = SourceConfig()
        assert cfg.source == "webcam"
        assert cfg.mavlink_address == "udp://:14540"
        assert cfg.rtsp_url is None
        assert cfg.edge_ws == "ws://localhost:9000"
        assert cfg.video_path is None

    def test_custom_values(self):
        cfg = SourceConfig(source="tello", rtsp_url="rtsp://cam", video_path="/v.mp4")
        assert cfg.source == "tello"
        assert cfg.rtsp_url == "rtsp://cam"
        assert cfg.video_path == "/v.mp4"


class TestAppState:
    """Verify AppState dataclass defaults and field types."""

    def test_defaults(self):
        state = AppState()
        assert state.running is False
        assert state.fleet is None
        assert state.match_history == []
        assert state.drone_telemetry == {}
        assert state.fps == 0
        assert state.persons_detected == 0

    def test_independent_mutable_defaults(self):
        """Two AppState instances must not share the same list/dict."""
        a = AppState()
        b = AppState()
        a.match_history.append({"x": 1})
        assert len(b.match_history) == 0
        a.drone_telemetry["bat"] = 80
        assert "bat" not in b.drone_telemetry


class TestStateDictCompat:
    """Verify the dict-like backward-compatible accessor works."""

    def test_getitem(self):
        state = AppState()
        compat = _StateDictCompat.__new__(_StateDictCompat)
        # Patch to use a local state instead of the module singleton
        # by monkey-patching getattr -- but easier: just test the real module one
        from flightrisk.dashboard.state import _state, app_state
        app_state.fps = 42
        assert _state["fps"] == 42

    def test_setitem(self):
        from flightrisk.dashboard.state import _state, app_state
        _state["fps"] = 99
        assert app_state.fps == 99

    def test_get_default(self):
        from flightrisk.dashboard.state import _state
        assert _state.get("nonexistent_attr_xyz", "fallback") == "fallback"

    def test_contains(self):
        from flightrisk.dashboard.state import _state
        assert "running" in _state
        assert "nonexistent_attr_xyz" not in _state

    def test_getitem_raises_keyerror_for_missing(self):
        from flightrisk.dashboard.state import _state
        with pytest.raises(KeyError):
            _ = _state["nonexistent_attr_xyz"]


class TestAppStateThreadSafety:
    """Verify concurrent reads/writes to AppState don't corrupt data."""

    def test_concurrent_match_history_writes(self):
        """Multiple threads appending to match_history via the lock."""
        from flightrisk.dashboard.state import app_state, match_history_lock

        original = app_state.match_history
        app_state.match_history = []
        errors = []

        def writer(thread_id, count):
            try:
                for i in range(count):
                    with match_history_lock:
                        app_state.match_history.append({"tid": thread_id, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t, 50)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        assert len(app_state.match_history) == 200  # 4 threads * 50 writes
        app_state.match_history = original

    def test_concurrent_scalar_reads_writes(self):
        """Concurrent reads and writes to scalar fields under the GIL."""
        from flightrisk.dashboard.state import app_state

        original_fps = app_state.fps
        errors = []

        def writer():
            try:
                for i in range(100):
                    app_state.fps = float(i)
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(100):
                    _ = app_state.fps
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(2)]
        threads += [threading.Thread(target=reader) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        app_state.fps = original_fps

    def test_fleet_lock_serializes_access(self):
        """fleet_lock can serialize access to fleet operations."""
        from flightrisk.dashboard.state import fleet_lock

        counter = [0]

        def increment():
            with fleet_lock:
                val = counter[0]
                time.sleep(0.001)
                counter[0] = val + 1

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert counter[0] == 10
