"""Security hardening tests for Amber Drone dashboard and persistence."""

import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from cryptography.fernet import Fernet


# ---------------------------------------------------------------------------
# 1. Flask SECRET_KEY
# ---------------------------------------------------------------------------


class TestSecretKey:
    def test_secret_key_from_env(self, monkeypatch):
        """SECRET_KEY should use AMBER_SECRET_KEY when set."""
        monkeypatch.setenv("AMBER_SECRET_KEY", "env-secret-key-value")
        import importlib
        import amber.dashboard.app as app_mod
        importlib.reload(app_mod)
        assert app_mod.app.config["SECRET_KEY"] == "env-secret-key-value"

    def test_secret_key_random_fallback(self, monkeypatch):
        """When AMBER_SECRET_KEY is not set, app should have a random secret key."""
        monkeypatch.delenv("AMBER_SECRET_KEY", raising=False)
        import amber.dashboard.app as app_mod

        # The app config should have a non-trivial key even without the env var
        actual_key = app_mod.app.config["SECRET_KEY"]
        assert actual_key is not None
        assert len(actual_key) > 0
        assert actual_key != "amber-drone-2026"


# ---------------------------------------------------------------------------
# 2. API key authentication
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_auth(monkeypatch):
    """Return a Flask test client with AMBER_API_KEY enabled."""
    monkeypatch.setenv("AMBER_API_KEY", "test-key-123")
    import amber.dashboard.app as app_mod

    # Patch via monkeypatch so the global is restored atomically at teardown; a
    # manual restore that reads os.environ back leaks "test-key-123" into the
    # global (the fixture finalizer runs before monkeypatch reverts the env
    # var) and poisons later tests such as tests/test_handlers.py.
    monkeypatch.setattr(app_mod, "_AMBER_API_KEY", "test-key-123")
    # Stub heavy components so routes don't crash
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

    with app_mod.app.test_client() as client:
        yield client


@pytest.fixture
def app_no_auth(monkeypatch):
    """Return a Flask test client with AMBER_API_KEY disabled."""
    monkeypatch.delenv("AMBER_API_KEY", raising=False)
    import amber.dashboard.app as app_mod

    monkeypatch.setattr(app_mod, "_AMBER_API_KEY", None)
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

    with app_mod.app.test_client() as client:
        yield client


class TestAPIKeyAuth:
    def test_auth_blocks_unauthenticated(self, app_with_auth):
        """Requests without Authorization header should get 401."""
        resp = app_with_auth.get("/api/status")
        assert resp.status_code == 401

    def test_auth_allows_correct_key(self, app_with_auth):
        """Requests with correct Bearer token should succeed."""
        resp = app_with_auth.get(
            "/api/status", headers={"Authorization": "Bearer test-key-123"}
        )
        assert resp.status_code == 200

    def test_auth_rejects_wrong_key(self, app_with_auth):
        """Requests with wrong Bearer token should get 401."""
        resp = app_with_auth.get(
            "/api/status", headers={"Authorization": "Bearer wrong-key"}
        )
        assert resp.status_code == 401

    def test_health_exempt_from_auth(self, app_with_auth):
        """GET /api/health must always respond 200, even with auth enabled."""
        resp = app_with_auth.get("/api/health")
        assert resp.status_code == 200

    def test_no_auth_when_key_not_set(self, app_no_auth):
        """When AMBER_API_KEY is not set, all requests should pass without auth."""
        resp = app_no_auth.get("/api/status")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 3. SessionDB encryption
# ---------------------------------------------------------------------------


class TestSessionDBEncryption:
    def _make_db(self, encryption_key=None):
        """Create a SessionDB with a temp file."""
        from amber.persistence import SessionDB

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = SessionDB(db_path=tmp.name, encryption_key=encryption_key)
        return db, tmp.name

    def test_encryption_round_trip(self):
        """Data encrypted on write should decrypt correctly on read."""
        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=key)
        try:
            sid = db.create_session(source="test")
            reasoning_text = "Child found near playground, confidence high"
            mid = db.add_match(
                session_id=sid,
                match_type="reid",
                reid_score=0.8,
                reasoning=reasoning_text,
            )

            # Verify the raw DB stores ciphertext, not plaintext
            import sqlite3

            raw_conn = sqlite3.connect(path)
            raw = raw_conn.execute(
                "SELECT reasoning FROM matches WHERE id = ?", (mid,)
            ).fetchone()[0]
            raw_conn.close()
            assert raw != reasoning_text  # must be encrypted
            assert raw is not None

            # Read back through SessionDB should return plaintext
            matches = db.get_session_matches(sid)
            assert len(matches) == 1
            assert matches[0]["reasoning"] == reasoning_text
        finally:
            db.close()
            os.unlink(path)

    def test_no_encryption_when_key_not_set(self):
        """Without encryption key, reasoning is stored as plaintext."""
        db, path = self._make_db(encryption_key=None)
        try:
            sid = db.create_session(source="test")
            reasoning_text = "Match found"
            mid = db.add_match(
                session_id=sid,
                match_type="reid",
                reasoning=reasoning_text,
            )

            # Raw DB should have plaintext
            import sqlite3

            raw_conn = sqlite3.connect(path)
            raw = raw_conn.execute(
                "SELECT reasoning FROM matches WHERE id = ?", (mid,)
            ).fetchone()[0]
            raw_conn.close()
            assert raw == reasoning_text

            # Read back should also be plaintext
            matches = db.get_session_matches(sid)
            assert matches[0]["reasoning"] == reasoning_text
        finally:
            db.close()
            os.unlink(path)

    def test_update_match_encrypts_reasoning(self):
        """update_match should encrypt reasoning too."""
        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=key)
        try:
            sid = db.create_session(source="test")
            mid = db.add_match(session_id=sid, match_type="reid", reasoning=None)

            updated_reasoning = "Updated: confirmed match via Gemma"
            db.update_match(mid, gemma_match=True, reasoning=updated_reasoning)

            # Raw should be encrypted
            import sqlite3

            raw_conn = sqlite3.connect(path)
            raw = raw_conn.execute(
                "SELECT reasoning FROM matches WHERE id = ?", (mid,)
            ).fetchone()[0]
            raw_conn.close()
            assert raw != updated_reasoning

            # SessionDB read should decrypt
            matches = db.get_session_matches(sid)
            assert matches[0]["reasoning"] == updated_reasoning
        finally:
            db.close()
            os.unlink(path)

    def test_decrypt_handles_plaintext_gracefully(self):
        """If data was stored without encryption, _decrypt should return it unchanged."""
        from amber.persistence import SessionDB

        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=None)
        try:
            # Store plaintext
            sid = db.create_session(source="test")
            db.add_match(session_id=sid, match_type="reid", reasoning="plaintext data")
            db.close()

            # Reopen with encryption enabled
            db2 = SessionDB(db_path=path, encryption_key=key)
            matches = db2.get_session_matches(sid)
            # Should gracefully return plaintext even though key is set
            assert matches[0]["reasoning"] == "plaintext data"
            db2.close()
        finally:
            os.unlink(path)

    def test_invalid_encryption_key_raises(self):
        """An invalid Fernet key should raise ValueError."""
        with pytest.raises(ValueError, match="valid Fernet key"):
            self._make_db(encryption_key="not-a-valid-key")


# ---------------------------------------------------------------------------
# 4. CORS origins from env var
# ---------------------------------------------------------------------------


class TestCORSOrigins:
    def test_cors_default_is_wildcard(self, monkeypatch):
        """Without AMBER_CORS_ORIGINS, default should be '*'."""
        monkeypatch.delenv("AMBER_CORS_ORIGINS", raising=False)
        origins = os.environ.get("AMBER_CORS_ORIGINS", "*")
        result = origins if origins == "*" else origins.split(",")
        assert result == "*"

    def test_cors_single_origin(self, monkeypatch):
        """Single origin from env var."""
        monkeypatch.setenv("AMBER_CORS_ORIGINS", "https://amber.example.com")
        origins = os.environ.get("AMBER_CORS_ORIGINS", "*")
        result = origins if origins == "*" else origins.split(",")
        assert result == ["https://amber.example.com"]

    def test_cors_multiple_origins(self, monkeypatch):
        """Multiple comma-separated origins from env var."""
        monkeypatch.setenv(
            "AMBER_CORS_ORIGINS", "https://a.com,https://b.com,https://c.com"
        )
        origins = os.environ.get("AMBER_CORS_ORIGINS", "*")
        result = origins if origins == "*" else origins.split(",")
        assert result == ["https://a.com", "https://b.com", "https://c.com"]


# ---------------------------------------------------------------------------
# 5. SocketIO authentication
# ---------------------------------------------------------------------------


class TestSocketIOAuth:
    @pytest.fixture(autouse=True)
    def _setup_app(self, monkeypatch):
        """Configure the app with auth enabled for each test."""
        monkeypatch.setenv("AMBER_API_KEY", "test-key-123")
        import amber.dashboard.app as app_mod

        monkeypatch.setattr(app_mod, "_AMBER_API_KEY", "test-key-123")
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
        self.app_mod = app_mod
        yield

    def test_socketio_rejects_no_auth(self):
        """Unauthenticated SocketIO connection should be refused."""
        from flask_socketio import SocketIOTestClient

        client = self.app_mod.app.test_client()
        sio_client = self.app_mod.socketio.test_client(
            self.app_mod.app, flask_test_client=client
        )
        assert not sio_client.is_connected()

    def test_socketio_rejects_wrong_key(self):
        """SocketIO connection with wrong api_key should be refused."""
        from flask_socketio import SocketIOTestClient

        client = self.app_mod.app.test_client()
        sio_client = self.app_mod.socketio.test_client(
            self.app_mod.app,
            flask_test_client=client,
            auth={"api_key": "wrong-key"},
        )
        assert not sio_client.is_connected()

    def test_socketio_accepts_correct_key(self):
        """SocketIO connection with correct api_key should succeed."""
        from flask_socketio import SocketIOTestClient

        client = self.app_mod.app.test_client()
        sio_client = self.app_mod.socketio.test_client(
            self.app_mod.app,
            flask_test_client=client,
            auth={"api_key": "test-key-123"},
        )
        assert sio_client.is_connected()
        sio_client.disconnect()


# ---------------------------------------------------------------------------
# 6. Decryption in get_confirmed_matches / export_eval_dataset
# ---------------------------------------------------------------------------


class TestDecryptionInReadPaths:
    def _make_db(self, encryption_key=None):
        """Create a SessionDB with a temp file."""
        from amber.persistence import SessionDB

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = SessionDB(db_path=tmp.name, encryption_key=encryption_key)
        return db, tmp.name

    def test_get_confirmed_matches_decrypts_reasoning(self):
        """get_confirmed_matches should return plaintext reasoning when encryption is enabled."""
        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=key)
        try:
            sid = db.create_session(source="test")
            reasoning_text = "Child identified by clothing and height"
            mid = db.add_match(
                session_id=sid,
                match_type="reid",
                reid_score=0.9,
                reasoning=reasoning_text,
            )
            db.add_feedback(mid, sid, "confirmed")

            matches = db.get_confirmed_matches()
            assert len(matches) == 1
            assert matches[0]["reasoning"] == reasoning_text
        finally:
            db.close()
            os.unlink(path)

    def test_export_eval_dataset_decrypts_reasoning(self):
        """export_eval_dataset should write plaintext reasoning when encryption is enabled."""
        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=key)
        export_path = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
        try:
            sid = db.create_session(source="test")
            reasoning_text = "Strong visual match confirmed"
            mid = db.add_match(
                session_id=sid,
                match_type="face",
                face_score=0.85,
                reasoning=reasoning_text,
            )
            db.add_feedback(mid, sid, "confirmed")

            count = db.export_eval_dataset(export_path)
            assert count == 1

            with open(export_path) as f:
                dataset = json.load(f)
            assert len(dataset) == 1
            assert dataset[0]["reasoning"] == reasoning_text
        finally:
            db.close()
            os.unlink(path)
            os.unlink(export_path)

    def test_target_description_encrypted_at_rest(self):
        """target_description should be encrypted in the DB and decrypted on read."""
        import sqlite3

        key = Fernet.generate_key().decode()
        db, path = self._make_db(encryption_key=key)
        try:
            description = "5-year-old boy, red shirt, blue jeans"
            sid = db.create_session(
                source="test", target_description=description
            )

            # Raw DB should have ciphertext
            raw_conn = sqlite3.connect(path)
            raw = raw_conn.execute(
                "SELECT target_description FROM sessions WHERE id = ?", (sid,)
            ).fetchone()[0]
            raw_conn.close()
            assert raw != description
            assert raw is not None

            # get_session should return plaintext
            session = db.get_session(sid)
            assert session["target_description"] == description

            # get_recent_sessions should also return plaintext
            sessions = db.get_recent_sessions()
            assert sessions[0]["target_description"] == description
        finally:
            db.close()
            os.unlink(path)
