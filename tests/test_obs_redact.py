"""Redaction, length caps and the content-capture switch (flightrisk/obs_redact.py)."""

import pytest

from flightrisk.obs_redact import (
    MAX_NOTES_CHARS,
    REDACTED,
    cap,
    content_capture_enabled,
    is_secret_key,
    redact_text,
    safe_token,
    sanitize,
    sanitize_fields,
)

FAKE_ANTHROPIC = "sk-ant-api03-FAKEfakeFAKEfake0123456789abcdef"


class TestRedactText:
    @pytest.mark.parametrize("secret", [
        FAKE_ANTHROPIC,
        "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "AIzaSyA-abcdefghijklmnopqrstuvwxyz01234",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ])
    def test_known_key_patterns(self, secret):
        out = redact_text(f"before {secret} after")
        assert secret not in out
        assert REDACTED in out
        assert out.startswith("before ") and out.endswith(" after")

    def test_bearer_basic_and_authorization_header(self):
        out = redact_text("Authorization: Basic dXNlcjpwYXNz, then Bearer abcdef123456 and more")
        assert "dXNlcjpwYXNz" not in out
        assert "abcdef123456" not in out
        assert "]]" not in out  # no artefacts from overlapping rules

    def test_token_style_pairs_but_not_token_counts(self):
        out = redact_text('token=hunter22 edge_token: s3cr3t {"api_key": "zzz"} input_tokens=512 token_count=3')
        assert "hunter22" not in out and "s3cr3t" not in out and "zzz" not in out
        assert "input_tokens=512" in out
        assert "token_count=3" in out

    def test_values_of_secret_environment_variables(self, monkeypatch):
        monkeypatch.setenv("FLIGHTRISK_EDGE_TOKEN", "plain-edge-token-value")
        monkeypatch.setenv("FLIGHTRISK_API_KEY", "dashboard-key-42")
        out = redact_text("ws auth plain-edge-token-value and key dashboard-key-42")
        assert "plain-edge-token-value" not in out
        assert "dashboard-key-42" not in out

    def test_cap(self):
        assert len(redact_text("x" * 5000)) <= 500
        assert cap("abc", 10) == "abc"
        assert cap("a" * 100, 20).endswith("...[truncated]")
        assert redact_text("y" * 600, None) == "y" * 600

    def test_never_raises_on_odd_input(self):
        class Bad:
            def __str__(self):
                raise RuntimeError("boom")

        assert redact_text(Bad()) == "<unprintable>"
        assert redact_text(12345) == "12345"


class TestSecretKeys:
    @pytest.mark.parametrize("key", ["api_key", "edge_token", "x-api-key", "Authorization", "password", "auth", "secret"])
    def test_credential_names(self, key):
        assert is_secret_key(key)

    @pytest.mark.parametrize("key", ["input_tokens", "token_count", "authenticated", "tokens", "description", "key_source"])
    def test_ordinary_names(self, key):
        assert not is_secret_key(key)


class TestSanitize:
    def test_content_off_keeps_tokens_numbers_and_lengths_only(self):
        payload = {"description": "red coat, blue cap", "threshold": 0.5, "preset": "balanced",
                   "notes": "seen near the lake", "flag": True, "drone_id": "drone-1", "free": "hello there world"}
        out = sanitize(payload, content=False)
        assert out["description"] == {"chars": 18}
        assert out["notes"] == {"chars": 18}
        assert out["free"] == {"chars": 17}
        assert out["threshold"] == 0.5
        assert out["preset"] == "balanced"
        assert out["drone_id"] == "drone-1"
        assert out["flag"] is True

    def test_content_on_keeps_redacted_capped_text(self):
        out = sanitize({"description": f"red coat {FAKE_ANTHROPIC}", "notes": "n" * 2000}, content=True)
        assert out["description"] == f"red coat {REDACTED}"
        assert len(out["notes"]) <= MAX_NOTES_CHARS

    def test_images_and_embeddings_are_never_kept(self):
        out = sanitize({"image": "A" * 4000, "photo": b"\xff\xd8" * 100, "reid_embedding": [0.1] * 512}, content=True)
        assert out["image"] == {"b64_chars": 4000, "approx_bytes": 3000}
        assert out["photo"] == {"bytes": 200}
        assert out["reid_embedding"] == {"numbers": 512}

    def test_secret_keys_redacted_whatever_the_switch(self):
        for content in (False, True):
            out = sanitize({"auth": {"api_key": "k" * 40}, "token": "abc"}, content=content)
            assert out["auth"] == REDACTED
            assert out["token"] == REDACTED

    def test_flags_under_secret_names_survive(self):
        assert sanitize({"has_token": True}, content=False) == {"has_token": True}

    def test_sanitize_fields_keeps_code_strings(self):
        out = sanitize_fields({"error": f"call failed with {FAKE_ANTHROPIC}", "outcome": "not executed now",
                               "description": "secret text"}, content=False)
        assert out["error"] == f"call failed with {REDACTED}"
        assert out["outcome"] == "not executed now"
        assert out["description"] == {"chars": 11}


def test_content_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("FLIGHTRISK_TELEMETRY_CONTENT", raising=False)
    assert content_capture_enabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", value)
        assert content_capture_enabled() is True
    monkeypatch.setenv("FLIGHTRISK_TELEMETRY_CONTENT", "0")
    assert content_capture_enabled() is False


def test_safe_token_bounds_label_values():
    assert safe_token("drone-1") == "drone-1"
    assert safe_token("free text with spaces") == "unknown"
    assert safe_token(None, "none") == "none"
    assert safe_token(7) == "7"
