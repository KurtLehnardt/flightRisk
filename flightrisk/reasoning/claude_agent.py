"""Claude reasoning agent via the Anthropic API.

Drop-in alternative to the Ollama/Gemma ``FlightRiskAgent``: same methods
(``analyze_match``, ``match_description``, ...), only the model call differs.

Cloud use is opt-in: the agent refuses to start unless
``FLIGHTRISK_ALLOW_CLOUD_LLM=1`` is set, because every call sends the detected
person's crop and the missing child's reference photo to Anthropic.

The API key is read only from ``FLIGHTRISK_CLAUDE_API_KEY``. The SDK's usual
``ANTHROPIC_API_KEY`` is deliberately ignored so a key exported for other tools
never enables uploads by accident. The key is never logged or included in
error results. The client is built with every setting explicit, so
``ANTHROPIC_BASE_URL``, ``ANTHROPIC_CUSTOM_HEADERS`` and the SDK's tracing
content mode cannot redirect the key or copy the photos elsewhere, and it
refuses to start if a header from the environment or a different credential
would still be sent. The SDK's tracing records metadata only and never
propagates FlightRisk's trace context to Anthropic; the operator's
``ANTHROPIC_OPEN_TELEMETRY=false`` still turns the SDK's tracing off.

How one call behaves (all of it in ``_ask`` and ``_send``):

* The request is streamed. Each read from the connection may wait at most
  30 s, so a stalled connection fails fast while a slow reply that keeps
  sending data is allowed to finish.
* A wall-clock deadline (``FLIGHTRISK_CLAUDE_DEADLINE``, default 90 s) ends the
  call while the reply streams, even when data keeps arriving: it is checked on
  every chunk read and every event. Setting up the request is bounded only by
  its own timeouts, not by the deadline: the DNS lookup (no timeout of its
  own), connecting (5 s), the upload (10 s per write) and the wait for the
  response headers (30 s). So a call can end later than the deadline by that
  setup time, or by one read timeout when the connection goes silent just
  before the deadline.
* The SDK's own retries are off. ``_ask`` retries once, only after a
  connection error or a 429/529 response, waits at most 2 s first, and only
  when the wait plus a connect and a read timeout still fit inside the
  deadline. It never retries after a read timeout or the deadline.
* A circuit breaker and a per-minute cap skip calls before anything is sent.
  A skipped call returns ``confidence="skipped"`` with plain operator text.
* A rejected key (HTTP 401 or 403) turns the agent off until it is rebuilt.
* Only a reply that ended normally (``end_turn`` or ``stop_sequence``) gives a
  verdict; a reply cut off for any other reason is an error.
"""

import functools
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import numpy as np

from flightrisk.config import get_config
from flightrisk.obs_log import notice
from flightrisk.obs_redact import redact_text
from flightrisk.reasoning.agent import (
    KEY_REJECTED_TEXT,
    PAUSED_TEXT,
    RATE_CAP_TEXT,
    FlightRiskAgent,
    ReasoningCallSkipped,
    ReasoningUnavailable,
    safe_type_name,
)
from flightrisk.vision.resize import downscale_long_edge

_log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (Claude API only).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Thinking tokens count toward max_tokens. The visible reply is three short
# lines (well under 200 tokens), so the rest is headroom for adaptive thinking
# at high effort. The limit also has to fit inside the call deadline: at a
# typical streaming rate of 50 to 100 output tokens per second, 4096 tokens take
# about 40 to 80 s, inside the 90 s default, where 8192 could not finish in
# time. If thinking does use it all, the reply has no text
# (stop_reason=max_tokens) and the result is an error rather than a hang.
_MAX_TOKENS = 4096

# Per-read limits for the streamed call. The read timeout applies to each chunk
# read from the connection, not to the whole reply, so a reply that keeps
# streaming (including the API's keep-alive pings) is not cut off by it; the
# call deadline bounds the total. 30 s leaves room for long gaps between chunks
# while the model thinks with its thinking hidden. The real gap length has not
# been measured against the live API yet (a follow-up smoke test).
_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 30.0
_WRITE_TIMEOUT_S = 10.0
_POOL_TIMEOUT_S = 5.0

# The one retry in _ask waits for the server's retry-after when it gives one,
# but never longer than this. The SDK's own retries are off because they follow
# retry-after with no upper limit.
_RETRY_WAIT_CAP_S = 2.0
_RETRY_DEFAULT_WAIT_S = 1.0

# Long-edge cap for images sent to the cloud. This is a deliberate privacy
# trade-off: 768 px keeps enough detail to compare clothing, hair and build,
# while sending Anthropic a much smaller copy of the child's reference photo
# (and of bystanders in the crop) than the original. Smaller uploads are also
# faster and cheaper.
_MAX_IMAGE_EDGE = 768

_RATE_WINDOW_S = 60.0

# Accepted ranges for the hardening settings; values outside are clamped.
_DEFAULT_COOLOFF_S = 60.0
_COOLOFF_RANGE_S = (1.0, 3600.0)
_DEFAULT_DEADLINE_S = 90.0
_DEADLINE_RANGE_S = (10.0, 600.0)

# Accepted effort levels; anything else falls back to the default with a warning.
_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_DEFAULT_EFFORT = "high"

# A verdict is read only from a reply that ended normally. Any other stop
# reason (max_tokens, pause_turn, none at all, ...) may have cut the reply off.
_VERDICT_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

# Header names the SDK itself sends. After the client is built, any other
# header name means something from the environment got through.
_SDK_HEADER_NAMES = frozenset({"accept", "content-type", "user-agent", "authorization", "anthropic-version"})

DEFAULT_BASE_URL = "https://api.anthropic.com"
BASE_URL_ENV = "FLIGHTRISK_CLAUDE_BASE_URL"
API_KEY_ENV = "FLIGHTRISK_CLAUDE_API_KEY"

# Result texts (see agent.py): fixed wording plus status codes, short codes
# and integers only, never an exception's message.
RATE_CAP_MESSAGE = RATE_CAP_TEXT
BREAKER_OPEN_MESSAGE = PAUSED_TEXT.replace("{label}", "Claude")
KEY_REJECTED_MESSAGE = KEY_REJECTED_TEXT.format(label="Claude")
DEADLINE_MESSAGE = "AI check timed out (no answer from Claude within {seconds} s)."
DECLINED_MESSAGE = "AI check failed (Claude declined the request: {category})."
NO_TEXT_MESSAGE = "AI check failed (Claude returned no text: {stop_reason})."
STATUS_ERROR_MESSAGE = "AI check failed (Claude error {status}: {label})."
STREAM_ERROR_MESSAGE = "AI check failed (Claude stream error: {label})."
STALLED_MESSAGE = "AI check failed (Claude sent no data for {seconds} s)."
UNREACHABLE_MESSAGE = "AI check failed (cannot reach Claude)."

_STATUS_LABELS = {
    400: "bad request", 401: "key rejected", 403: "access denied", 404: "not found",
    408: "timed out", 409: "conflict", 413: "request too large", 422: "invalid request",
    429: "rate limited", 500: "server error", 502: "bad gateway", 503: "unavailable",
    504: "timed out", 529: "overloaded",
}
_BODY_TYPE_LABELS = {
    "overloaded_error": "overloaded", "api_error": "server error", "rate_limit_error": "rate limited",
    "invalid_request_error": "bad request", "authentication_error": "key rejected",
    "permission_error": "access denied", "not_found_error": "not found",
    "request_too_large": "request too large",
}
# API-provided codes (a refusal category, a stop reason) are shown only if they
# look like a short code.
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

# Error types an SSE ``error`` event can carry mid-stream that mean the API is unhealthy.
_OUTAGE_BODY_TYPES = {"overloaded_error", "api_error", "rate_limit_error"}


class ClaudeClientRefused(RuntimeError):
    """The SDK client would send a header from the environment or a different credential."""


class ClaudeCallSkipped(ReasoningCallSkipped):
    """A Claude call was skipped locally (breaker open or rate cap); nothing was sent."""


class ClaudeKeyRejected(ReasoningUnavailable):
    """The API rejected the key (HTTP 401 or 403); the agent is now unavailable."""


class ClaudeDeadlineExceeded(RuntimeError):
    """The call ran past its wall-clock deadline of ``seconds`` and the stream was closed."""

    def __init__(self, seconds: int):
        super().__init__(f"deadline of {seconds} s passed")
        self.seconds = seconds


class ClaudeNoVerdict(RuntimeError):
    """The API answered, but with a refusal (``kind="refusal"``) or without text (``"no_text"``).

    ``detail`` is the refusal category or stop reason when it is a short code, else None.
    """

    def __init__(self, kind: str, detail: Any = None):
        self.kind = kind
        self.detail = detail if isinstance(detail, str) and _CODE_RE.fullmatch(detail) else None
        super().__init__(f"{kind} ({self.detail or 'unknown'})")


def _type_names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _body_error_type(exc: BaseException) -> str | None:
    """The ``error.type`` of an API error body, such as ``overloaded_error``."""
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    kind = error.get("type") if isinstance(error, dict) else None
    return kind if isinstance(kind, str) else None


def _is_auth_rejection(exc: BaseException) -> bool:
    return _status(exc) in (401, 403)


def _is_read_timeout(exc: BaseException) -> bool:
    """True for a timeout while waiting for data (not for a connect timeout)."""
    names = _type_names(exc)
    if "APITimeoutError" in names:
        cause = exc.__cause__
        return not (cause is not None and "ConnectTimeout" in _type_names(cause))
    return bool(names & {"TimeoutException", "TimeoutError"})


def _is_outage(exc: BaseException) -> bool:
    """True for failures that mean the API is unreachable or unhealthy: these open the breaker.

    A call that hit its deadline is not one: the API was answering, just slowly.
    """
    if isinstance(exc, ClaudeDeadlineExceeded):
        return False
    status = _status(exc)
    if status is not None and (status == 429 or status >= 500):
        return True
    if _body_error_type(exc) in _OUTAGE_BODY_TYPES:
        return True
    if _is_read_timeout(exc):
        return True  # a stall: nothing arrived within the per-read timeout
    return bool(_type_names(exc) & {"APIConnectionError", "TransportError", "ConnectionError"})


def _api_answered(exc: BaseException) -> bool:
    """True when the API was reachable and answered (a 4xx such as 400, a refusal, no text)."""
    if isinstance(exc, ClaudeNoVerdict):
        return True
    status = _status(exc)
    return status is not None and 400 <= status < 500 and status not in (401, 403, 429)


def _retry_after(exc: BaseException) -> float:
    """The server's retry-after for ``exc`` in seconds, capped at ``_RETRY_WAIT_CAP_S``."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    wait = None
    try:
        if headers is not None:
            if headers.get("retry-after-ms") is not None:
                wait = float(headers.get("retry-after-ms")) / 1000.0
            elif headers.get("retry-after") is not None:
                wait = float(headers.get("retry-after"))
    except (TypeError, ValueError):
        wait = None  # an HTTP date or garbage: use the default
    if wait is None or not math.isfinite(wait) or wait < 0:
        wait = _RETRY_DEFAULT_WAIT_S
    return min(wait, _RETRY_WAIT_CAP_S)


def _retry_wait(exc: BaseException, started: bool) -> float | None:
    """Seconds to wait before the single retry, or None when ``exc`` must not be retried.

    Only a connection error or a 429/529 before the reply started is retried.
    A read timeout, the deadline, or anything after the stream began is not:
    the API may already be working on (and billing) that request.
    """
    if started or isinstance(exc, ClaudeDeadlineExceeded) or _is_read_timeout(exc):
        return None
    status = _status(exc)
    if status in (429, 529):
        return _retry_after(exc)
    if status is None and "APIConnectionError" in _type_names(exc):
        return _RETRY_DEFAULT_WAIT_S
    return None


def _verdict_text(content: Any) -> str:
    """The reply text: only text blocks after the last ``fallback`` block.

    With server-side fallback, a model that declined part-way hands over at a
    ``fallback`` block, and any text before it belongs to the declined attempt,
    so it must not mix into the verdict. Thinking blocks are never included.
    """
    blocks = list(content or [])
    start = 0
    for i, block in enumerate(blocks):
        if getattr(block, "type", None) == "fallback":
            start = i + 1
    return "".join(b.text for b in blocks[start:] if getattr(b, "type", None) == "text").strip()


def _clamped(setting: str, value: Any, lo: float, hi: float | None, default: float) -> float:
    """``value`` as a number in ``[lo, hi]``; NaN or junk becomes ``default``. Warns when it changes it."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = math.nan
    if math.isnan(number) or (hi is None and number == math.inf):
        used = default  # nothing sensible to clamp to
    elif hi is not None and number > hi:
        used = hi
    elif number < lo:
        used = lo
    else:
        return number
    _log.warning("claude_setting_clamped setting=%s value=%r used=%s min=%s max=%s",
                 setting, value, used, lo, hi)
    return used


@functools.cache
def _deadline_body_type() -> type:
    """The response-body wrapper that enforces the deadline on every chunk (built once)."""
    import httpx2

    class DeadlineBody(httpx2.SyncByteStream):
        """A streamed response body that stops being read once the call's deadline has passed.

        The SDK drops the API's keep-alive pings before ``_send`` sees an
        event, so a reply that only sends pings (for example while the model
        thinks with its thinking hidden) would never reach the per-event check.
        Checking here, on every chunk read from the connection, bounds that
        case too. It follows the same pattern as the SDK's own tracing wrapper.
        """

        def __init__(self, body, deadline: float, clock: Callable[[], float], seconds: int):
            self._body = body
            self._deadline = deadline
            self._clock = clock
            self._seconds = seconds
            observer = getattr(body, "stream_observer", None)
            if observer is not None:
                # The SDK finds its tracing observer on response.stream.
                self.stream_observer = observer

        @property
        def elapsed(self):
            return getattr(self._body, "elapsed", None)

        def __iter__(self):
            for chunk in self._body:
                if self._clock() >= self._deadline:
                    raise ClaudeDeadlineExceeded(self._seconds)
                yield chunk

        def close(self) -> None:
            self._body.close()

    return DeadlineBody


def _install_deadline(stream: Any, deadline: float, clock: Callable[[], float], seconds: int) -> bool:
    """Wrap the stream's response body so every chunk read checks the deadline.

    Returns False (and changes nothing) when the stream has no real httpx2
    body, as with a test double; the per-event check in ``_send`` still applies.
    """
    try:
        import httpx2
    except ImportError:
        return False
    response = getattr(stream, "response", None)
    body = getattr(response, "stream", None)
    if not isinstance(body, httpx2.SyncByteStream):
        return False
    response.stream = _deadline_body_type()(body, deadline, clock, seconds)
    return True


def _valid_effort(value: Any) -> str:
    """``value`` as an effort level, or the default (with a warning) when it is not one."""
    effort = value.strip().lower() if isinstance(value, str) else ""
    if effort in _EFFORT_LEVELS:
        return effort
    _log.warning("claude_setting_invalid setting=FLIGHTRISK_CLAUDE_EFFORT value=%s used=%s allowed=%s",
                 redact_text(value, 64), _DEFAULT_EFFORT, ",".join(_EFFORT_LEVELS))
    return _DEFAULT_EFFORT


def downscale_for_upload(image: np.ndarray, max_edge: int = _MAX_IMAGE_EDGE) -> np.ndarray:
    """Shrink ``image`` to the cloud upload cap (long edge ``max_edge``); never upscales."""
    return downscale_long_edge(image, max_edge)


def resolve_api_key() -> str | None:
    """The Claude API key from ``FLIGHTRISK_CLAUDE_API_KEY`` (empty counts as unset)."""
    return os.environ.get(API_KEY_ENV) or None


def resolve_base_url() -> str:
    """The API base URL: Anthropic's, unless ``FLIGHTRISK_CLAUDE_BASE_URL`` names an https URL.

    ``ANTHROPIC_BASE_URL`` is never used. An override that is not https, or
    that carries credentials, is refused so the key is never sent in clear
    text or to an address the operator did not choose for FlightRisk.
    """
    raw = (os.environ.get(BASE_URL_ENV) or "").strip()
    if not raw:
        return DEFAULT_BASE_URL
    parts = urlsplit(raw)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise RuntimeError(
            f"{BASE_URL_ENV} must be an https:// URL without credentials; "
            "the Claude API key is only ever sent over https."
        )
    return raw


def _open_telemetry_option() -> dict[str, Any]:
    """The SDK tracing option: metadata only, no trace propagation, and the operator's opt-out kept.

    Passing an option makes the SDK ignore its environment variables, so
    ``ANTHROPIC_OPEN_TELEMETRY_TRACES_CONTENT_MODE=content`` can never put the
    photos into trace spans. ``ANTHROPIC_OPEN_TELEMETRY=false`` is read here
    and still turns the SDK's tracing off. Propagation is always off, so
    FlightRisk's trace context is never sent to Anthropic.
    """
    traces: dict[str, Any] = {"content_mode": "metadata_only"}
    if os.environ.get("ANTHROPIC_OPEN_TELEMETRY", "").strip().lower() == "false":
        traces["enabled"] = False
    return {"traces": traces, "propagation": False}


def _env_custom_headers() -> dict[str, str]:
    """``ANTHROPIC_CUSTOM_HEADERS`` parsed the way the SDK parses it, names lower-cased."""
    parsed: dict[str, str] = {}
    for line in (os.environ.get("ANTHROPIC_CUSTOM_HEADERS") or "").split("\n"):
        colon = line.find(":")
        if colon >= 0:
            parsed[line[:colon].strip().lower()] = line[colon + 1:].strip()
    return parsed


def _verify_headers(client: Any, api_key: str) -> None:
    """Fail closed unless the client sends only the SDK's own headers and our key.

    Raises ClaudeClientRefused (naming headers, never values) when a header
    name outside the SDK's own set is present, when a header from
    ``ANTHROPIC_CUSTOM_HEADERS`` kept its value, or when the auth header is
    anything but ``Bearer <FLIGHTRISK_CLAUDE_API_KEY>``.
    """
    # The same name can appear twice with different case (the SDK's own and an
    # environment copy), so every value of each name is checked.
    headers: dict[str, list[Any]] = {}
    for name, value in dict(client.default_headers).items():
        headers.setdefault(str(name).lower(), []).append(value)
    unexpected = {k for k in headers if k not in _SDK_HEADER_NAMES and not k.startswith("x-stainless-")}
    leaked = {k for k, v in _env_custom_headers().items() if v in headers.get(k, [])}
    bad = sorted(unexpected | leaked)
    if bad:
        raise ClaudeClientRefused(
            "Refusing to start Claude: request headers from the environment could not be removed "
            f"({', '.join(redact_text(k, 64) for k in bad)}). Unset ANTHROPIC_CUSTOM_HEADERS for FlightRisk.")
    if headers.get("authorization") != [f"Bearer {api_key}"] or "x-api-key" in headers:
        raise ClaudeClientRefused(
            f"Refusing to start Claude: requests would not authenticate with {API_KEY_ENV}.")


def _build_client(anthropic: Any, api_key: str, base_url: str, *, read_timeout: float = _READ_TIMEOUT_S) -> Any:
    """The SDK client with every setting passed explicitly, so no ``ANTHROPIC_*`` variable applies.

    Never copy this client (``with_options``, ``copy``): a copy is built by the
    SDK's constructor again, which re-reads ``ANTHROPIC_CUSTOM_HEADERS`` and
    would bring the headers removed here back. Build a new one with this
    function instead.
    """
    client = anthropic.Anthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=anthropic.Timeout(
            connect=_CONNECT_TIMEOUT_S, read=read_timeout, write=_WRITE_TIMEOUT_S, pool=_POOL_TIMEOUT_S),
        max_retries=0,
        default_headers={},
        open_telemetry=_open_telemetry_option(),
    )
    # The SDK merges ANTHROPIC_CUSTOM_HEADERS into its headers even when
    # default_headers is passed, and those headers override the auth header.
    # Reset them so no header comes from the environment, then check that
    # nothing but the SDK's own headers and our key is left.
    client._custom_headers = {}
    _verify_headers(client, api_key)
    return client


class ClaudeReasoningAgent(FlightRiskAgent):
    """Person-matching reasoning backed by Claude (vision)."""

    provider = "claude"
    label = "Claude"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        effort: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        from flightrisk.reasoning import cloud_block_reason

        blocked = cloud_block_reason(self.provider)
        if blocked:
            raise RuntimeError(blocked)

        key = api_key or resolve_api_key()
        if not key:
            raise RuntimeError(
                f"{API_KEY_ENV} is not set. Add it to your environment "
                "(ANTHROPIC_API_KEY is not used), or set "
                "FLIGHTRISK_REASONING_PROVIDER=gemma to use local Ollama."
            )
        key_source = "api_key argument" if api_key else API_KEY_ENV
        base_url = resolve_base_url()

        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic") from e

        cfg = get_config().reasoning
        self.model = model or cfg.claude_model
        self.effort = _valid_effort(effort or cfg.claude_effort)
        self._anthropic = anthropic
        self._clock = clock
        self._sleep = sleep
        self._key_source = key_source
        self._read_timeout_s = _READ_TIMEOUT_S
        try:
            self._client = _build_client(anthropic, key, base_url)
        except ClaudeClientRefused:
            raise  # its text names headers and variables, never values
        except Exception as e:
            # The message goes to the dashboard's provider switch; keep the
            # SDK's text (which may carry request details) in the redacted log.
            _log.warning("claude_client_setup_failed error_type=%s error=%s", safe_type_name(e), redact_text(e))
            raise RuntimeError(f"Could not set up the Claude client ({safe_type_name(e)}).") from None
        self._breaker_threshold = int(_clamped(
            "FLIGHTRISK_CLAUDE_BREAKER_THRESHOLD", cfg.claude_breaker_threshold, 1, None, 3))
        self._breaker_cooloff = _clamped(
            "FLIGHTRISK_CLAUDE_BREAKER_COOLOFF", cfg.claude_breaker_cooloff, *_COOLOFF_RANGE_S, _DEFAULT_COOLOFF_S)
        self._max_calls_per_minute = int(_clamped(
            "FLIGHTRISK_CLAUDE_MAX_CALLS_PER_MINUTE", cfg.claude_max_calls_per_minute, 1, None, 20))
        self._deadline_s = _clamped(
            "FLIGHTRISK_CLAUDE_DEADLINE", cfg.claude_deadline, *_DEADLINE_RANGE_S, _DEFAULT_DEADLINE_S)
        self._guard = threading.Lock()
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._call_times: list[float] = []
        self._key_rejected = False
        self._available = True
        self.last_usage: dict[str, Any] = {}
        host = urlsplit(base_url).hostname
        # Say where the key came from (never the key itself) and that data
        # leaves the device, so the operator can see cloud use is active.
        notice(
            _log, "reasoning_ready",
            f"[reasoning] Claude ({self.model}) ready; API key from {key_source}"
            + ("" if base_url == DEFAULT_BASE_URL else f"; API host {host} (from {BASE_URL_ENV})")
            + ". Cloud reasoning is ON: person crops and the reference photo are sent to Anthropic.",
            provider=self.provider, model=self.model, key_source=key_source, cloud=True, api_host=host,
        )

    def _image_to_base64(self, image: np.ndarray) -> str:
        """Downscale to the upload cap, then encode as base64 JPEG."""
        return super()._image_to_base64(downscale_for_upload(image))

    def health(self) -> dict[str, Any]:
        """Breaker and rate-cap state for the dashboard (no secrets)."""
        now = self._clock()
        with self._guard:
            left = max(0.0, self._open_until - now)
            recent = sum(1 for t in self._call_times if now - t < _RATE_WINDOW_S)
            failures = self._consecutive_failures
            rejected = self._key_rejected
        return {
            "breaker": "open" if left > 0 else "closed",
            "breaker_seconds_left": math.ceil(left),
            "consecutive_failures": failures,
            "calls_last_minute": recent,
            "max_calls_per_minute": self._max_calls_per_minute,
            "key_rejected": rejected,
        }

    def _admit(self) -> None:
        """Raise ClaudeCallSkipped if the breaker is open or the minute cap is hit.

        Runs before any image is resized or encoded. The cap counts logical
        calls: the single retry inside ``_ask`` belongs to the same call and
        does not take another slot.
        """
        now = self._clock()
        with self._guard:
            if now < self._open_until:
                raise ClaudeCallSkipped("paused", retry_in_s=math.ceil(self._open_until - now))
            # After the cool-off the next call goes out as the probe. There is
            # no strict half-open state limiting it to one probe at a time,
            # because one serial reasoning worker makes every call, so only one
            # can be in flight. The failure count stays at the threshold, so a
            # failed probe reopens the breaker at once.
            self._call_times = [t for t in self._call_times if now - t < _RATE_WINDOW_S]
            if len(self._call_times) >= self._max_calls_per_minute:
                raise ClaudeCallSkipped("rate_cap")
            self._call_times.append(now)

    def _record_failure(self, exc: Exception) -> None:
        """Update the breaker for a failed call.

        An outage (connection error, read timeout, 429, 5xx, 529) counts
        toward opening it. A reply from a reachable API (a 400, a refusal, no
        text) resets the count. A call that hit its deadline, or an unexpected
        local error, leaves the count as it is.
        """
        with self._guard:
            if _is_outage(exc):
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._breaker_threshold:
                    self._open_until = self._clock() + self._breaker_cooloff
                    _log.warning(
                        "claude_breaker_open failures=%d cooloff_s=%.0f",
                        self._consecutive_failures, self._breaker_cooloff)
            elif _api_answered(exc):
                self._consecutive_failures = 0

    def _record_success(self) -> None:
        with self._guard:
            self._consecutive_failures = 0
            self._open_until = 0.0

    def _reject_key(self, exc: Exception) -> None:
        """Turn the agent off after a 401/403. A new agent (restart or re-selecting Claude) clears it."""
        status = _status(exc)
        with self._guard:
            first = not self._key_rejected
            self._key_rejected = True
        self._unavailable_reason = KEY_REJECTED_MESSAGE
        self._available = False
        if first:
            notice(
                _log, "reasoning_key_rejected",
                f"[reasoning] Claude rejected the API key from {self._key_source} (HTTP {status}). "
                "AI checks are off until the key is fixed and the dashboard restarts or Claude is selected again.",
                level=logging.ERROR, provider=self.provider, model=self.model, status=status,
                key_source=self._key_source,
            )

    def _ask(self, prompt: str, images_b64: list[str]) -> str:
        """One logical call: a streamed request, at most one retry, all inside the deadline.

        ``_admit`` has already run. Only the final outcome of the call updates
        the breaker, so a call and its retry count as one failure at most.
        """
        deadline = self._clock() + self._deadline_s
        try:
            started = [False]
            try:
                text = self._send(prompt, images_b64, deadline, started)
            except Exception as first:
                wait = _retry_wait(first, started[0])
                # Retry only if the retry could still connect and get a first
                # chunk before the deadline.
                needed = 0.0 if wait is None else wait + _CONNECT_TIMEOUT_S + self._read_timeout_s
                if wait is None or self._clock() + needed >= deadline:
                    raise
                _log.info("claude_call_retry error_type=%s wait_s=%.1f", type(first).__name__, wait)
                self._sleep(wait)
                started = [False]
                text = self._send(prompt, images_b64, deadline, started)
        except Exception as e:
            if _is_auth_rejection(e):
                self._reject_key(e)
                # The SDK error carries the response, never the key; drop it anyway.
                raise ClaudeKeyRejected("key_rejected") from None
            self._record_failure(e)
            raise
        self._record_success()
        return text

    def _send(self, prompt: str, images_b64: list[str], deadline: float, started: list[bool]) -> str:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
            for b64 in images_b64
        ]
        content.append({"type": "text", "text": prompt})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "output_config": {"effort": self.effort},
            "messages": [{"role": "user", "content": content}],
        }
        if self.model in _FALLBACK_MODELS:
            # A classifier decline is re-run server-side on a fallback model
            # instead of surfacing as a failed match check.
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        seconds = round(self._deadline_s)
        # Leaving the with block (normally or by an exception) closes the stream.
        with self._client.beta.messages.stream(**kwargs) as stream:
            started[0] = True
            _install_deadline(stream, deadline, self._clock, seconds)
            for _event in stream:
                if self._clock() >= deadline:
                    raise ClaudeDeadlineExceeded(seconds)
            response = stream.get_final_message()

        usage = getattr(response, "usage", None)
        self.last_usage = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

        if response.stop_reason == "refusal":
            raise ClaudeNoVerdict("refusal", getattr(response.stop_details, "category", None))
        if response.stop_reason not in _VERDICT_STOP_REASONS:
            # max_tokens, pause_turn or no stop reason: the reply may be cut off
            # mid-verdict, so it is not trusted even if it has text.
            raise ClaudeNoVerdict("no_text", response.stop_reason)

        text = _verdict_text(response.content)
        if not text:
            raise ClaudeNoVerdict("no_text", response.stop_reason)
        return text

    def _error_text(self, exc: Exception) -> str:
        """Operator text for a failed Claude call, from codes and fixed labels only (never ``str(exc)``)."""
        if isinstance(exc, ClaudeDeadlineExceeded):
            return DEADLINE_MESSAGE.format(seconds=exc.seconds)
        if isinstance(exc, ClaudeNoVerdict):
            if exc.kind == "refusal":
                return DECLINED_MESSAGE.format(category=exc.detail or "policy")
            return NO_TEXT_MESSAGE.format(stop_reason=exc.detail or "unknown")
        status = _status(exc)
        if status is not None and status >= 400:
            label = _STATUS_LABELS.get(status) or ("server error" if status >= 500 else "request error")
            return STATUS_ERROR_MESSAGE.format(status=status, label=label)
        body_type = _body_error_type(exc)
        if body_type is not None:
            return STREAM_ERROR_MESSAGE.format(label=_BODY_TYPE_LABELS.get(body_type, "error"))
        if _is_read_timeout(exc):
            return STALLED_MESSAGE.format(seconds=round(_READ_TIMEOUT_S))
        if _type_names(exc) & {"APIConnectionError", "TransportError", "ConnectionError"}:
            return UNREACHABLE_MESSAGE
        return super()._error_text(exc)
