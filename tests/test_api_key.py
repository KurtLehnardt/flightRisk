"""Tests for the ``require_api_key`` decorator that gates REST endpoints.

Complements ``tests/test_security.py::TestAPIKeyAuth``. These focus on the
decorator design introduced in Phase 2: only data/mutation REST routes are
gated, while the root HTML page (``index``), ``/api/health``, and static
assets stay open so the dashboard still loads in a browser when a key is set.
"""

import os
from unittest.mock import MagicMock

import pytest


def _stub_state(app_mod):
    """Stub heavy pipeline components so routes don't crash under test."""
    app_mod._state["db"] = MagicMock()
    app_mod._state["metrics"] = MagicMock()
    app_mod._state["source"] = "webcam"
    app_mod._state["running"] = False
    app_mod._state["target_photo"] = None
    app_mod._state["target_description"] = None
    app_mod._state["reasoning"] = None
    app_mod._state["face"] = None
    app_mod._state["fps"] = 0
    app_mod._state["persons_detected"] = 0
    app_mod._state["drone_telemetry"] = {}
    app_mod._state["match_history"] = []


@pytest.fixture
def app_with_key(monkeypatch):
    """Flask test client with AMBER_API_KEY configured (auth enforced)."""
    monkeypatch.setenv("AMBER_API_KEY", "test-key-123")
    import amber.dashboard.app as app_mod

    app_mod._AMBER_API_KEY = "test-key-123"
    _stub_state(app_mod)
    with app_mod.app.test_client() as client:
        yield app_mod, client
    app_mod._AMBER_API_KEY = os.environ.get("AMBER_API_KEY") or None


@pytest.fixture
def app_no_key(monkeypatch):
    """Flask test client with AMBER_API_KEY unset (auth disabled / dev mode)."""
    monkeypatch.delenv("AMBER_API_KEY", raising=False)
    import amber.dashboard.app as app_mod

    app_mod._AMBER_API_KEY = None
    _stub_state(app_mod)
    with app_mod.app.test_client() as client:
        yield app_mod, client


class TestRequireApiKeyDecorator:
    def test_protected_mutation_route_blocked_without_key(self, app_with_key):
        """A gated mutation endpoint returns 401 JSON when the key is missing."""
        _, client = app_with_key
        resp = client.post("/api/export-eval-dataset")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"

    def test_protected_mutation_route_allowed_with_key(self, app_with_key):
        """The same endpoint succeeds when the correct Bearer key is supplied."""
        app_mod, client = app_with_key
        app_mod._state["db"].export_eval_dataset.return_value = 3
        resp = client.post(
            "/api/export-eval-dataset",
            headers={"Authorization": "Bearer test-key-123"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_protected_data_route_rejects_wrong_key(self, app_with_key):
        """A data-exposing endpoint rejects a wrong key before the view runs."""
        _, client = app_with_key
        resp = client.get("/api/sessions", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_index_not_gated_when_key_set(self, app_with_key):
        """Root HTML page stays reachable so the dashboard loads in a browser."""
        _, client = app_with_key
        resp = client.get("/")
        assert resp.status_code == 200

    def test_health_not_gated_when_key_set(self, app_with_key):
        """/api/health is a ping route and must never require the key."""
        _, client = app_with_key
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_protected_route_open_when_no_key(self, app_no_key):
        """With no key configured, gated routes pass through unauthenticated."""
        app_mod, client = app_no_key
        app_mod._state["db"].get_recent_sessions.return_value = []
        resp = client.get("/api/sessions")
        assert resp.status_code == 200
