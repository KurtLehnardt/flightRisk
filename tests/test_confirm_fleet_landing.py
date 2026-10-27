"""Confirm on a multi-drone (edge fleet) ground station lands the whole fleet."""

from __future__ import annotations

from unittest.mock import MagicMock

from flightrisk.dashboard import app as app_module
from flightrisk.dashboard.alert_latch import AlertPhase, alert_latch
from flightrisk.dashboard.recovery import confirm_recovery, handle_alert_response
from flightrisk.dashboard.state import app_state


class _Sio:
    def __init__(self):
        self.events = []

    def emit(self, name, payload=None, **kwargs):
        self.events.append((name, payload))


def _open_alert() -> str:
    aid = alert_latch.try_open(["grid:1_1"])
    assert aid is not None
    alert_latch.attach(aid, {"alert_id": aid, "track_id": "1_1"})
    return aid


def test_confirm_without_primary_drone_lands_fleet_through_land_all(clean_app_state, monkeypatch):
    app_state.fleet = None
    app_state.fleet_supervisor = MagicMock()
    app_state.db = None
    calls = []
    monkeypatch.setattr(app_module, "_land_all", lambda: calls.append("land_all") or {"reached_any": True})
    aid = _open_alert()
    sio = _Sio()

    body, status = handle_alert_response(aid, "confirm", sio)

    assert status == 200 and body["recovery_started"] is True
    confirm_recovery.thread.join(timeout=2)
    assert calls == ["land_all"]
    assert alert_latch.phase is AlertPhase.RESOLVED
    stages = [p["stage"] for n, p in sio.events if n == "recovery_status"]
    assert stages == ["landing", "landed"]


def test_confirm_with_no_drone_and_no_fleet_still_reports_no_drone(clean_app_state, monkeypatch):
    app_state.fleet = None
    app_state.fleet_supervisor = None
    app_state.db = None
    monkeypatch.setattr(app_module, "_land_all", lambda: (_ for _ in ()).throw(AssertionError("no fleet to land")))
    aid = _open_alert()
    sio = _Sio()

    handle_alert_response(aid, "confirm", sio)
    confirm_recovery.thread.join(timeout=2)

    assert [p["stage"] for n, p in sio.events if n == "recovery_status"] == ["no_drone"]
