"""Tests for the fail-closed ``before_request`` API-key gate on REST endpoints.

Complements ``tests/test_security.py::TestAPIKeyAuth``. Option C replaced the
Phase 2 per-route ``@require_api_key`` allow-list decorator with a
deny-by-default ``@app.before_request`` hook: EVERY route is gated when a key is
set, except the exempt public paths (the root HTML page ``/``, ``/api/health``,
and static assets) so the dashboard still loads in a browser. The observable
contract is unchanged (data routes 401 without a key / work with the correct
key; index + health stay reachable; everything open when the key is unset), and
``test_unlisted_route_protected_by_default`` additionally proves the fail-closed
property that motivated Option C.
"""

from unittest.mock import MagicMock

import pytest

import amber.dashboard.app as _app_mod

# Register a throwaway, auth-decorator-free route at IMPORT time -- before the
# shared Flask app handles its first request, after which Flask locks further
# setup (``add_url_rule`` inside a test would raise). This route is on no
# allow-list and carries no ``@require_api_key`` decorator, so it exists purely
# to prove the fail-closed property exercised by
# ``test_unlisted_route_protected_by_default``.
if "_test_unlisted" not in _app_mod.app.view_functions:
    _app_mod.app.add_url_rule(
        "/api/_test_unlisted", "_test_unlisted", lambda: ("ok", 200)
    )


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

    # Patch the module global via monkeypatch so it is restored atomically at
    # teardown. A manual save/restore leaks here: the fixture finalizer runs
    # before monkeypatch reverts the env var, so reading os.environ back would
    # re-store "test-key-123" into the global and poison later tests (e.g. the
    # unauthenticated SocketIO connect in tests/test_handlers.py).
    monkeypatch.setattr(app_mod, "_AMBER_API_KEY", "test-key-123")
    _stub_state(app_mod)
    with app_mod.app.test_client() as client:
        yield app_mod, client


@pytest.fixture
def app_no_key(monkeypatch):
    """Flask test client with AMBER_API_KEY unset (auth disabled / dev mode)."""
    monkeypatch.delenv("AMBER_API_KEY", raising=False)
    import amber.dashboard.app as app_mod

    monkeypatch.setattr(app_mod, "_AMBER_API_KEY", None)
    _stub_state(app_mod)
    with app_mod.app.test_client() as client:
        yield app_mod, client


class TestApiKeyGate:
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

    def test_unlisted_route_protected_by_default(self, app_with_key):
        """Fail-closed proof: a route with NO explicit auth is STILL gated.

        This is the whole reason for Option C. ``/api/_test_unlisted`` (see the
        module-level registration) carries no ``@require_api_key`` decorator and
        is on no allow-list. Under the old per-route decorator this endpoint
        would have been wide open; under the ``before_request`` gate it is
        protected by default -- demonstrating routes are protected because they
        exist, not because someone remembered to add them to an allow-list.
        """
        _, client = app_with_key

        # No Authorization header: the gate must reject it even though nothing
        # explicitly opted this route into auth -- proving default-deny.
        resp = client.get("/api/_test_unlisted")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "unauthorized"

        # Correct Bearer key: the same route now succeeds, confirming the 401
        # above came from the auth gate and not a routing/500 error.
        resp = client.get(
            "/api/_test_unlisted",
            headers={"Authorization": "Bearer test-key-123"},
        )
        assert resp.status_code == 200
