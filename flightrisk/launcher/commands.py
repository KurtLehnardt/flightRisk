"""Command lines and environments of the processes the launcher starts, and the edge token.

The edge token is read from ``FLIGHTRISK_EDGE_TOKEN`` in the launcher's
environment, else from ``~/.flightrisk-fleet.env`` (lines such as
``export FLIGHTRISK_EDGE_TOKEN=...``). It is passed to child processes only
through their environment. It is never put on a command line, and
``Redactor`` removes it from any text the launcher shows or writes.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from flightrisk.launcher.settings import LauncherSettings

TOKEN_ENV = "FLIGHTRISK_EDGE_TOKEN"
FLEET_ENV_FILE = Path.home() / ".flightrisk-fleet.env"
REDACTED = "[redacted]"

#: Variables the launcher sets itself (or must not inherit), removed from the inherited environment
#: first. An inherited FLIGHTRISK_GROUND_URLS, for example, would override the edge's --ws-url, and an
#: inherited FLIGHTRISK_TELLO_LOCAL_IP would bind a drone to an adapter the operator did not pick.
CONTROLLED_ENV = (
    TOKEN_ENV,
    "FLIGHTRISK_GROUND_URLS",
    "FLIGHTRISK_GROUND_LOCAL",
    "FLIGHTRISK_TELLO_LOCAL_IP",
    "FLIGHTRISK_TELLO_IFINDEX",
    "FLIGHTRISK_DETECTION_PROFILE",
    "FLIGHTRISK_NO_LLM",
    "FLIGHTRISK_STREAM_VIDEO",
    "FLIGHTRISK_FLEET_DRONES",
    "FLIGHTRISK_FLEET_NET",
    "FLIGHTRISK_EDGE_HOST",
    "FLIGHTRISK_EDGE_PORT",
    "FLIGHTRISK_EDGE_WS",
    "FLIGHTRISK_PORT",
    "FLIGHTRISK_DASHBOARD_HOST",
    "FLIGHTRISK_SOURCE",
    "FLIGHTRISK_GEOFENCE_CM",
    "FLIGHTRISK_CEILING_CM",
    "FLIGHTRISK_CEILING_MARGIN_CM",
    "FLIGHTRISK_APPEARANCE_ONLY",
    "FLIGHTRISK_SAVE_CAPTURES",
    "FLIGHTRISK_OTEL_ENABLED",
    "OTEL_EXPORTER",
    "OTEL_ENDPOINT",
    "OTEL_RESOURCE_ATTRIBUTES",
)


# ---------------------------------------------------------------------------
# Edge token
# ---------------------------------------------------------------------------


class EdgeToken:
    """The edge token and where it came from. ``repr`` and ``str`` never show the value."""

    __slots__ = ("_value", "source")

    def __init__(self, value: str | None, source: str | None):
        self._value = value or None
        self.source = source if self._value else None

    @property
    def status(self) -> str:
        """``set`` or ``missing``; the only thing the window ever shows about the token."""
        return "set" if self._value else "missing"

    def reveal(self) -> str | None:
        """The value, for a child process's environment and for redaction only."""
        return self._value

    def __repr__(self) -> str:
        return f"EdgeToken(status={self.status!r}, source={self.source!r})"

    __str__ = __repr__


def parse_env_file(text: str) -> dict[str, str]:
    """``KEY=value`` lines (``export`` allowed, ``#`` comments, optional quotes) as a dict."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load_token(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> EdgeToken:
    """The edge token from the environment, else from ``~/.flightrisk-fleet.env``."""
    env = os.environ if environ is None else environ
    value = (env.get(TOKEN_ENV) or "").strip()
    if value:
        return EdgeToken(value, "environment")
    path = FLEET_ENV_FILE if env_file is None else env_file
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return EdgeToken(None, None)
    value = parse_env_file(text).get(TOKEN_ENV, "").strip()
    return EdgeToken(value, f"~/{path.name}" if path.parent == Path.home() else str(path)) if value else EdgeToken(None, None)


class Redactor:
    """Replaces every occurrence of the edge token (and its shell-quoted form) with ``[redacted]``.

    ``add`` registers a further token (the file may change while children run
    with the old one); every token ever added stays redacted.
    """

    def __init__(self, token: EdgeToken | str | None = None):
        self._needles: tuple[str, ...] = ()
        self.add(token)

    def add(self, token: EdgeToken | str | None) -> None:
        value = token.reveal() if isinstance(token, EdgeToken) else token
        if not value:
            return
        needles = set(self._needles) | {value, shlex.quote(value)}
        self._needles = tuple(sorted(needles, key=len, reverse=True))

    def __call__(self, text: str) -> str:
        for needle in self._needles:
            if needle in text:
                text = text.replace(needle, REDACTED)
        return text

    def bytes(self, data: bytes) -> bytes:
        for needle in self._needles:
            raw = needle.encode("utf-8")
            if raw in data:
                data = data.replace(raw, REDACTED.encode("utf-8"))
        return data


# ---------------------------------------------------------------------------
# Process specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessSpec:
    """One child process: its name, command line, environment and when it counts as ready.

    ``ready_ports`` are (host, port) pairs that must accept a TCP connection
    before the next process starts (the ground's dashboard and edge link).
    ``env`` holds the full child environment and may contain the token; it is
    never shown. ``display_env`` lists the variables the launcher set, for the
    window, without the token.
    """

    name: str
    kind: str
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(repr=False)
    display_env: Mapping[str, str] = field(default_factory=dict)
    ready_ports: tuple[tuple[str, int], ...] = ()
    drone_id: str | None = None

    def command_line(self) -> str:
        return " ".join(shlex.quote(part) for part in self.argv)


def _otel_env(settings: LauncherSettings, instance: str) -> dict[str, str]:
    if not settings.otel_enabled:
        return {"FLIGHTRISK_OTEL_ENABLED": "0"}
    endpoint = settings.otel_endpoint.strip()
    exporter = "otlp-http" if endpoint.rstrip("/").endswith(":4318") else "otlp-grpc"
    return {
        "FLIGHTRISK_OTEL_ENABLED": "1",
        "OTEL_EXPORTER": exporter,
        "OTEL_ENDPOINT": endpoint,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.instance.id={instance}",
    }


def _child_env(base: Mapping[str, str], extra: Mapping[str, str], token: EdgeToken) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in CONTROLLED_ENV}
    env.update(extra)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    value = token.reveal()
    if value:
        env[TOKEN_ENV] = value
    return env


def ground_spec(settings: LauncherSettings, token: EdgeToken, python: str, base_env: Mapping[str, str]) -> ProcessSpec:
    """The ground station and dashboard (``python -m flightrisk.dashboard --source edge``)."""
    argv = [
        python, "-m", "flightrisk.dashboard",
        "--source", "edge",
        "--edge-host", ",".join(settings.edge_bind_hosts()),
        "--edge-port", str(settings.edge_port),
        "--host", "127.0.0.1",
        "--port", str(settings.dashboard_port),
        # Exactly the selected drones: a listed drone that never connects would still get a zone.
        "--fleet", ",".join(settings.fleet_ids()),
        "--appearance-only",
    ]
    if not settings.llm:
        argv.append("--no-llm")
    if settings.stream_video:
        argv.append("--stream-video")
    extra = {
        # The cable layout is the AP path of docs/fleet-setup.md: the ground then refuses any
        # bind address that is not a literal, so it can never listen on a drone's open Wi-Fi.
        "FLIGHTRISK_FLEET_NET": "ap" if settings.network == "cable" else "station",
        "FLIGHTRISK_APPEARANCE_ONLY": "1",
        "FLIGHTRISK_SAVE_CAPTURES": "0",
        **_otel_env(settings, "ground"),
    }
    return ProcessSpec(
        name="ground",
        kind="ground",
        argv=tuple(argv),
        env=_child_env(base_env, extra, token),
        display_env=dict(extra),
        ready_ports=(("127.0.0.1", settings.dashboard_port), ("127.0.0.1", settings.edge_port)),
    )


def edge_spec(settings: LauncherSettings, index: int, token: EdgeToken, python: str, base_env: Mapping[str, str]) -> ProcessSpec:
    """One edge (``python -m flightrisk.edge_device``) for ``settings.drones[index]``."""
    drone = settings.drones[index]
    argv = [python, "-m", "flightrisk.edge_device", "--source", drone.source]
    if drone.source == "tello":
        argv += ["--tello-host", drone.tello_host]
        if drone.local_ip:
            argv += ["--tello-local-ip", drone.local_ip]
            if drone.ifindex is not None:
                argv += ["--tello-ifindex", str(drone.ifindex)]
    argv += [
        "--drone-id", drone.drone_id,
        "--alt-slot", str(drone.alt_slot_cm),
        "--ws-url", settings.edge_ws_url(),
        "--detection-profile", settings.detector_profile,
        "--appearance-only",
    ]
    if settings.stream_video:
        argv.append("--stream-video")
    extra = {
        "FLIGHTRISK_GEOFENCE_CM": str(settings.geofence_cm),
        "FLIGHTRISK_CEILING_CM": str(settings.ceiling_cm),
        "FLIGHTRISK_CEILING_MARGIN_CM": str(settings.margin_cm),
        "FLIGHTRISK_APPEARANCE_ONLY": "1",
        **_otel_env(settings, f"edge-{drone.drone_id}"),
    }
    return ProcessSpec(
        name=f"edge-{drone.drone_id}",
        kind="edge",
        argv=tuple(argv),
        env=_child_env(base_env, extra, token),
        display_env=dict(extra),
        drone_id=drone.drone_id,
    )


def build_plan(
    settings: LauncherSettings,
    token: EdgeToken,
    python: str,
    base_env: Mapping[str, str] | None = None,
) -> list[ProcessSpec]:
    """The processes to start, in order: the ground first (when it runs here), then each local edge."""
    env = dict(os.environ if base_env is None else base_env)
    plan: list[ProcessSpec] = []
    if settings.runs_ground:
        plan.append(ground_spec(settings, token, python, env))
    local = settings.local_drones()
    for index, drone in enumerate(settings.drones):
        if any(drone is other for other in local):
            plan.append(edge_spec(settings, index, token, python, env))
    return plan
