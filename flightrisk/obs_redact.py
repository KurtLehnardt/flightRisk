"""Redaction, length caps and the content-capture switch for telemetry.

Everything FlightRisk writes to logs, span attributes or metric labels passes
through this module first. It has three jobs:

* **Secrets never leave the process.** :func:`redact_text` replaces API keys,
  bearer tokens, ``Authorization`` headers, the values of
  ``FLIGHTRISK_EDGE_TOKEN`` / ``FLIGHTRISK_API_KEY`` /
  ``FLIGHTRISK_CLAUDE_API_KEY`` (and similar variables) and anything that
  matches a known key pattern (``sk-ant-...``, ``sk-...``, ``ghp_...``,
  ``AKIA...``, ``AIza...``, JWTs) with ``[REDACTED]``. Mapping keys whose
  name looks like a credential (``token``, ``api_key``, ``password``, ...)
  are redacted whatever their value.
* **Fields stay small.** Strings are capped (``MAX_FIELD_CHARS`` by default,
  500 for operator notes), lists and mappings are truncated, and binary or
  base64 image payloads are replaced by their size.
* **Operator content is opt-in.** :func:`content_capture_enabled` reads
  ``FLIGHTRISK_TELEMETRY_CONTENT`` (default off). With it off, free text the
  operator typed (a target description, alert notes) is reduced to its
  length, and only short control tokens (a preset name, a command, a drone
  id) are kept. With it on, the text is kept after redaction and capping.

The functions here are pure and cheap; they never raise on odd input.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any

#: Environment switch for logging operator text and storing the target image.
CONTENT_ENV = "FLIGHTRISK_TELEMETRY_CONTENT"

#: Default cap for one string field in a log line.
MAX_FIELD_CHARS = 500

#: Cap for operator notes (alert responses, match feedback).
MAX_NOTES_CHARS = 500

#: At most this many items of a list or mapping are kept.
MAX_ITEMS = 32

#: Nesting depth kept when sanitizing payloads.
MAX_DEPTH = 4

REDACTED = "[REDACTED]"

_TRUE = frozenset({"1", "true", "yes", "on"})

#: Environment variables whose values are secrets wherever they appear.
SECRET_ENV_VARS = (
    "FLIGHTRISK_EDGE_TOKEN",
    "FLIGHTRISK_API_KEY",
    "FLIGHTRISK_CLAUDE_API_KEY",
    "FLIGHTRISK_SECRET_KEY",
    "FLIGHTRISK_ENCRYPTION_KEY",
    "FLIGHTRISK_PREVIEW_SECRET",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GRAFANA_ADMIN_PASSWORD",
)

# Known credential shapes. Order matters only for readability.
_KEY_PATTERNS = (
    r"sk-ant-[A-Za-z0-9_\-]{6,}",            # Anthropic
    r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}",     # OpenAI-style
    r"gh[pousr]_[A-Za-z0-9]{20,}",            # GitHub
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"AKIA[0-9A-Z]{16}",                      # AWS access key id
    r"AIza[0-9A-Za-z_\-]{30,}",               # Google API key
    r"xox[abpors]-[A-Za-z0-9\-]{10,}",        # Slack
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",  # JWT
)
_KEY_RE = re.compile("|".join(_KEY_PATTERNS))

# "Bearer <anything>" and "Authorization: <anything>" up to whitespace/quote.
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{4,}")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+(?:\s+[^\s,;]+)?)")

# key=value / "key": "value" pairs whose key names a credential.
_SECRET_WORDS = r"(?:token|secret|password|passwd|pwd|api[_\-]?key|apikey|auth[_\-]?key|private[_\-]?key|access[_\-]?key|session[_\-]?key|credentials?|authorization|auth|cookie)"
# A key is a credential when its last segment is one of the words above, so
# ``edge_token`` and ``x-api-key`` are, but ``input_tokens`` and ``token_count`` are not.
_SECRET_NAME = r"(?:[A-Za-z0-9]+[_\-])*" + _SECRET_WORDS
_KV_RE = re.compile(
    r"(?i)(?P<key>(?<![A-Za-z0-9_\-])[\"']?" + _SECRET_NAME + r"[\"']?\s*[:=]\s*)(?!\[REDACTED\])(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)

#: Mapping keys whose value is redacted whatever it is.
_SECRET_KEY_RE = re.compile(r"(?i)^(?:set-cookie|" + _SECRET_NAME + r")$")

#: Keys whose (string) value is image or binary data: summarized, never logged.
BINARY_KEYS = frozenset({"image", "photo", "frame", "thumbnail", "snapshot", "crop", "data_url", "file", "jpeg", "png"})

#: Keys whose string value is free operator text: kept only with content capture on.
FREE_TEXT_KEYS = frozenset({"description", "notes", "note", "comment", "text", "query"})

#: Short control tokens kept even with content capture off.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,64}$")


def content_capture_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``FLIGHTRISK_TELEMETRY_CONTENT`` is 1/true/yes/on (default off)."""
    env = os.environ if environ is None else environ
    return str(env.get(CONTENT_ENV, "")).strip().lower() in _TRUE


def _secret_values(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    values = []
    for name in SECRET_ENV_VARS:
        value = env.get(name)
        # Very short values would redact ordinary words; real secrets are longer.
        if value and len(value) >= 6:
            values.append(value)
    return values


def cap(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    """``text`` cut to ``limit`` characters, marking the cut."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)] + "...[truncated]"


def redact_text(text: Any, limit: int | None = MAX_FIELD_CHARS) -> str:
    """Return ``text`` as a string with every known secret replaced by ``[REDACTED]``.

    Applies the exact values of the secret environment variables first, then
    the key patterns, bearer/basic credentials, ``Authorization`` headers and
    ``token=...``-style pairs. The result is capped to ``limit`` characters
    (``None`` for no cap). Never raises.
    """
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:  # noqa: BLE001 -- a broken __str__ must not break logging
        return "<unprintable>"
    if not s:
        return s
    for secret in _secret_values():
        if secret in s:
            s = s.replace(secret, REDACTED)
    s = _KEY_RE.sub(REDACTED, s)
    s = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", s)
    s = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
    s = _KV_RE.sub(lambda m: f"{m.group('key')}{REDACTED}", s)
    if limit is not None:
        s = cap(s, limit)
    return s


def is_secret_key(key: Any) -> bool:
    """True when a mapping key names a credential (``token``, ``api_key``, ...)."""
    return isinstance(key, str) and bool(_SECRET_KEY_RE.match(key))


def _binary_summary(value: Any) -> dict[str, Any]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": len(value)}
    if isinstance(value, str):
        # base64 is 4 chars per 3 bytes; a data: URL prefix is negligible.
        return {"b64_chars": len(value), "approx_bytes": (len(value) * 3) // 4}
    return {"type": type(value).__name__}


def sanitize(
    value: Any,
    *,
    content: bool | None = None,
    key: str | None = None,
    depth: int = 0,
    limit: int = MAX_FIELD_CHARS,
) -> Any:
    """Return a JSON-friendly, redacted, size-capped copy of ``value``.

    ``content`` is the content-capture switch (``None`` reads the
    environment). ``key`` is the mapping key ``value`` was found under; it
    decides whether the value is a secret, binary data or free text.
    """
    if content is None:
        content = content_capture_enabled()
    if value is None or isinstance(value, bool):
        # A flag such as ``has_token`` is never itself a secret.
        return value
    if key is not None and is_secret_key(key):
        return REDACTED
    if isinstance(value, (int, float)):
        return value
    lowered = key.lower() if isinstance(key, str) else None
    if isinstance(value, (bytes, bytearray, memoryview)) or (lowered in BINARY_KEYS):
        return _binary_summary(value)
    if isinstance(value, str):
        if lowered in FREE_TEXT_KEYS:
            if not content:
                return {"chars": len(value)}
            return redact_text(value, MAX_NOTES_CHARS if lowered in ("notes", "note") else limit)
        if not content and not _TOKEN_RE.match(value):
            # Content capture off: only short control tokens survive.
            return {"chars": len(value)}
        return redact_text(value, limit)
    if depth >= MAX_DEPTH:
        return {"type": type(value).__name__}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= MAX_ITEMS:
                out["_truncated_keys"] = len(value) - MAX_ITEMS
                break
            out[str(k)[:64]] = sanitize(v, content=content, key=str(k), depth=depth + 1, limit=limit)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if items and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items) and len(items) > MAX_ITEMS:
            # A long numeric vector (an embedding, for instance) is never logged.
            return {"numbers": len(items)}
        kept = [sanitize(v, content=content, key=key, depth=depth + 1, limit=limit) for v in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            kept.append({"_truncated_items": len(items) - MAX_ITEMS})
        return kept
    return redact_text(value, limit) if content else {"type": type(value).__name__}


def sanitize_fields(fields: Mapping[str, Any], *, content: bool | None = None, limit: int = MAX_FIELD_CHARS) -> dict[str, Any]:
    """Sanitize every value of a log record's structured fields.

    Unlike :func:`sanitize`, plain string values that are not free text are
    kept (redacted and capped) even with content capture off: these are
    fields the code itself chose to log (event names, error messages,
    outcomes), not operator input.
    """
    if content is None:
        content = content_capture_enabled()
    out: dict[str, Any] = {}
    for k, v in fields.items():
        lowered = k.lower()
        if is_secret_key(k) and not (v is None or isinstance(v, bool)):
            out[k] = REDACTED
        elif isinstance(v, str) and lowered not in FREE_TEXT_KEYS and lowered not in BINARY_KEYS:
            out[k] = redact_text(v, limit)
        else:
            out[k] = sanitize(v, content=content, key=k, limit=limit)
    return out


def safe_token(value: Any, default: str = "unknown", limit: int = 64) -> str:
    """A bounded, label-safe token for metric labels (never free text)."""
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value[:limit]
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int):
        return str(value)
    return default


def first_present(mapping: Mapping[str, Any], keys: Iterable[str]) -> Any:
    """The value of the first key of ``keys`` present in ``mapping``."""
    for k in keys:
        if k in mapping:
            return mapping[k]
    return None


__all__ = [
    "BINARY_KEYS",
    "CONTENT_ENV",
    "FREE_TEXT_KEYS",
    "MAX_FIELD_CHARS",
    "MAX_NOTES_CHARS",
    "REDACTED",
    "SECRET_ENV_VARS",
    "cap",
    "content_capture_enabled",
    "first_present",
    "is_secret_key",
    "redact_text",
    "safe_token",
    "sanitize",
    "sanitize_fields",
]
