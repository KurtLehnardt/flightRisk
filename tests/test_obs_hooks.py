"""RED metrics and user_input events for the dashboard handlers (flightrisk/dashboard/obs_hooks.py)."""

import base64
import logging

import pytest

from flightrisk import obs_metrics as M
from flightrisk.dashboard import app as app_module

FAKE_KEY = "sk-ant-api03-FAKEfakeFAKEfake0123456789"


class Recorder:
    def __init__(self):
        self.calls = []

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    record = add


@pytest.fixture
def rec(monkeypatch):
    out = {}
    for name in ("http_requests", "http_duration", "socket_events", "socket_duration", "socket_emits"):
        out[name] = Recorder()
        monkeypatch.setattr(M, name, out[name])
    return out


def user_inputs(caplog):
    return [r.fr_fields for r in caplog.records if getattr(r, "fr_event", None) == "user_input"]


def test_rest_post_is_logged_with_redacted_notes(rec, caplog, clean_app_state, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "1")
    client = app_module.app.test_client()
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        resp = client.post("/api/alerts/nope/respond", json={"action": "dismiss", "notes": f"false alarm {FAKE_KEY}"})
    assert resp.status_code == 409
    ev = user_inputs(caplog)[-1]
    assert ev["action"] == "POST /api/alerts/<alert_id>/respond"
    assert ev["outcome"] == "rejected" and ev["status"] == 409 and ev["channel"] == "rest"
    assert ev["input"]["body"]["notes"] == "false alarm [REDACTED]"
    assert ev["input"]["path"] == {"alert_id": "nope"}
    assert FAKE_KEY not in caplog.text
    labels = rec["http_requests"].calls[-1][1]
    assert labels == {"route": "/api/alerts/<alert_id>/respond", "method": "POST", "status_class": "4xx", "outcome": "rejected"}
    assert rec["http_duration"].calls


def test_get_requests_are_counted_but_not_logged_as_input(rec, caplog, clean_app_state):
    client = app_module.app.test_client()
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        assert client.get("/api/health").status_code == 200
    assert user_inputs(caplog) == []
    assert rec["http_requests"].calls[-1][1]["route"] == "/api/health"


def test_api_key_gate_is_unchanged_and_refusals_are_logged(rec, caplog, clean_app_state, monkeypatch):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "the-dashboard-key-123")
    client = app_module.app.test_client()
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        refused = client.post("/api/clear-target", headers={"Authorization": "Bearer wrong-key-456"})
        public = client.get("/api/health")
    assert refused.status_code == 401
    assert public.status_code == 200
    ev = user_inputs(caplog)[-1]
    assert ev["outcome"] == "unauthorized" and ev["status"] == 401
    assert "wrong-key-456" not in caplog.text and "the-dashboard-key-123" not in caplog.text


@pytest.fixture
def sock(clean_app_state):
    client = app_module.socketio.test_client(app_module.app)
    yield client
    if client.is_connected():
        client.disconnect()


def test_socket_event_logged_without_text_when_content_off(rec, caplog, sock, monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_TELEMETRY_CONTENT", raising=False)
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        sock.emit("set_description", {"description": "small child, yellow raincoat"})
    ev = [e for e in user_inputs(caplog) if e["action"] == "set_description"][-1]
    assert ev["input"] == {"description": {"chars": 28}}
    assert ev["outcome"] == "ok" and ev["channel"] == "socket"
    assert "yellow raincoat" not in caplog.text
    assert ("set_description", "ok") in [(c[1]["event"], c[1]["outcome"]) for c in rec["socket_events"].calls]


def test_socket_handler_error_and_rejection_outcomes(rec, caplog, sock):
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        sock.emit("set_sensitivity_preset", {"preset": "no-such-preset"})  # answers with an error event
        with pytest.raises(ValueError):
            sock.emit("set_threshold", {"threshold": "not-a-number"})  # the handler raises, as before
    outcomes = {e["action"]: e["outcome"] for e in user_inputs(caplog)}
    assert outcomes["set_sensitivity_preset"] == "rejected"
    assert outcomes["set_threshold"] == "error"
    assert ("error", ) == tuple({c[1]["event"] for c in rec["socket_emits"].calls if c[1]["event"] == "error"})


def test_set_target_image_is_reduced_to_its_size(rec, caplog, sock, monkeypatch):
    monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "1")
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        sock.emit("set_target", {"image": base64.b64encode(b"not an image at all").decode()})
    ev = [e for e in user_inputs(caplog) if e["action"] == "set_target"][-1]
    assert set(ev["input"]["image"]) == {"b64_chars", "approx_bytes"}


def test_connect_auth_key_is_redacted(rec, caplog, clean_app_state, monkeypatch):
    monkeypatch.setattr(app_module, "_FLIGHTRISK_API_KEY", "socket-key-789xyz")
    with caplog.at_level(logging.INFO, logger="flightrisk"):
        client = app_module.socketio.test_client(app_module.app, auth={"api_key": "socket-key-789xyz"})
        assert client.is_connected()
        client.disconnect()
        denied = app_module.socketio.test_client(app_module.app, auth={"api_key": "wrong-key-000"})
        assert not denied.is_connected()
    connects = [e for e in user_inputs(caplog) if e["action"] == "connect"]
    assert [c["outcome"] for c in connects[-2:]] == ["ok", "denied"]
    assert connects[-1]["input"] == {"auth": "[REDACTED]"}
    assert "socket-key-789xyz" not in caplog.text and "wrong-key-000" not in caplog.text
