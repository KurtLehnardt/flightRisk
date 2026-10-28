"""Log rate limiting and auth-rejection backoff for the edge/ground link.

A misconfigured edge (wrong ``FLIGHTRISK_EDGE_TOKEN``) reconnects forever, and
both ends used to log every attempt. These small helpers keep the logs readable
and the retry pace sane without making the rejection fatal.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

# Close code the ground sends when the auth handshake fails. 4010 is the
# multi-ground uplink's protocol refusal (fleet_ground_link.CLOSE_PROTOCOL) and
# 4009 is a drone-id conflict, so auth keeps its own code.
CLOSE_UNAUTHORIZED = 4001
CLOSE_UNAUTHORIZED_REASON = "unauthorized: edge token mismatch"
CLOSE_UNAUTHORIZED_REASON_NO_MESSAGE = "unauthorized: no auth message"

AUTH_BACKOFF_BASE_S = 1.0
# Matches REFUSED_RECONNECT_MAX_S in edge_uplink: a ground that rejects this
# edge is retried at least every 10 s, so fixing the token on the ground is
# noticed within about 10 s.
AUTH_BACKOFF_MAX_S = 10.0
# Longest log text taken from a peer-controlled close reason.
MAX_REASON_CHARS = 120
# A connection must stay up this long before the backoff and log state reset.
STABLE_UPTIME_S = 10.0

AUTH_REJECTED_HINT = "check FLIGHTRISK_EDGE_TOKEN matches the ground"


def auth_backoff_s(rejections: int) -> float:
    """Delay before the next attempt after *rejections* consecutive auth rejections."""
    if rejections <= 0:
        return 0.0
    return min(AUTH_BACKOFF_BASE_S * 2 ** min(rejections - 1, 16), AUTH_BACKOFF_MAX_S)


class RateLimitedLog:
    """Decide whether a repeated message should be logged for a key.

    The first occurrence always logs; after that one in every *every*
    occurrences logs, or sooner if *min_interval_s* has passed since the last
    logged one. ``should_log`` returns how many occurrences this log line
    stands for (1 for the first), or 0 when the message should be dropped.
    """

    def __init__(
        self,
        every: int = 10,
        min_interval_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 256,
    ):
        self._every = max(1, every)
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._max_keys = max_keys
        # key -> [pending count, time of last logged occurrence]
        self._state: dict[object, list[float]] = {}

    def should_log(self, key: object = None) -> int:
        now = self._clock()
        entry = self._state.get(key)
        if entry is None:
            if len(self._state) >= self._max_keys:
                self._state.pop(next(iter(self._state)))
            self._state[key] = [0, now]
            return 1
        entry[0] += 1
        if entry[0] >= self._every or now - entry[1] >= self._min_interval_s:
            count = int(entry[0])
            entry[0], entry[1] = 0, now
            return count
        return 0

    def reset(self, key: object = None) -> None:
        self._state.pop(key, None)

    def reset_all(self) -> None:
        self._state.clear()


def safe_url(url: str) -> str:
    """*url* without any user name or password, for logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _safe_reason(reason: object) -> str:
    """A peer-controlled close reason as a quoted, escaped, truncated literal."""
    return repr(str(reason)[:MAX_REASON_CHARS])


def auth_rejected_line(ground: str, code: object, reason: str, attempts: int) -> str:
    """The single error line for an auth rejection; never includes the token or URL credentials."""
    return (
        f"edge_auth_rejected ground={safe_url(ground)} code={code} reason={_safe_reason(reason)} "
        f'attempts={attempts} hint="{AUTH_REJECTED_HINT}"'
    )


def closed_line(ground: str, code: object, reason: str, count: int) -> str:
    """The rate-limited 'closed' warning, with the close code and reason."""
    more = "" if count <= 1 else f" ({count} closes since last report)"
    return f"[{safe_url(ground)}] Connection to ground closed (code={code} reason={_safe_reason(reason)}){more}"
