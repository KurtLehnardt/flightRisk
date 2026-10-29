"""Launcher settings, presets and validation. No user-interface imports.

A ``LauncherSettings`` describes what this computer runs:

* role ``ground``: the ground station and dashboard run here. Every drone in
  ``drones`` is in the ground's ``--fleet`` list, and each drone runs its edge
  either on this computer (``run_here``) or on another computer that dials
  this one over the cable (layout A).
* role ``edges``: only edges run here, and they dial a ground station at
  ``ground_host``:``ground_port`` on another computer.

``validate`` returns every problem as an ``Issue``; an issue of level
``error`` blocks the start. Presets are stored in ``~/.flightrisk/launcher.json``
(``FLIGHTRISK_LAUNCHER_FILE`` overrides the path) and never contain the edge token.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from flightrisk.drone.safety import (
    MAX_ALT_SLOT_CM,
    MIN_ALT_SLOT_CM,
    slot_above_ceiling,
    validate_ceiling,
)
from flightrisk.fleet_protocol import is_identifier
from flightrisk.launcher.netinfo import Adapter, adapters_for_host

ROLES = ("ground", "edges")
NETWORKS = ("loopback", "cable")
SOURCES = ("tello", "fake")
MIN_DRONES = 1
MAX_DRONES = 4
DEFAULT_TELLO_HOST = "192.168.10.1"
DEFAULT_CABLE_ADDRESS = "10.42.0.1"
DEFAULT_DASHBOARD_PORT = 5555
DEFAULT_EDGE_PORT = 9000
DEFAULT_LAUNCHER_PORT = 5500
DEFAULT_GEOFENCE_CM = 450
DEFAULT_CEILING_CM = 200
DEFAULT_MARGIN_CM = 60
DEFAULT_OTEL_ENDPOINT = "http://localhost:4317"
#: The coordinator's altitude slots for zones in turn, so an edge starts at the slot it is planned at.
DEFAULT_SLOTS_CM = (100, 130)
MIN_PORT = 1024
MAX_PORT = 65535
LAUNCHER_FILE_ENV = "FLIGHTRISK_LAUNCHER_FILE"
_FILE_VERSION = 1
MAX_PRESET_NAME = 60
#: The largest geofence accepted, in cm (50 m); the indoor floor is about 4 m.
MAX_GEOFENCE_CM = 5000
MAX_TEXT_LEN = 256
#: Drone ids name log files and processes, so they are stricter than the protocol's identifiers: no
#: '.' or ':' (``edge-drone:1.log`` would be a hidden NTFS stream on Windows).
_DRONE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

ERROR = "error"
WARNING = "warning"


class SettingsError(ValueError):
    """A settings object from the window or a preset file has the wrong shape or type."""


@dataclass(frozen=True)
class Issue:
    """One validation finding. ``field`` names the setting, for example ``drones.1.local_ip``."""

    level: str
    field: str
    message: str

    def to_json(self) -> dict[str, str]:
        return {"level": self.level, "field": self.field, "message": self.message}


@dataclass(frozen=True)
class DroneSettings:
    """One drone and its edge process."""

    drone_id: str = "drone-1"
    source: str = "tello"
    tello_host: str = DEFAULT_TELLO_HOST
    local_ip: str = ""
    ifindex: int | None = None
    alt_slot_cm: int = DEFAULT_SLOTS_CM[0]
    run_here: bool = True


@dataclass(frozen=True)
class LauncherSettings:
    """Everything the operator picks before pressing Start."""

    role: str = "ground"
    network: str = "loopback"
    cable_address: str = DEFAULT_CABLE_ADDRESS
    drones: tuple[DroneSettings, ...] = field(default_factory=lambda: (DroneSettings(),))
    ground_host: str = DEFAULT_CABLE_ADDRESS
    ground_port: int = DEFAULT_EDGE_PORT
    dashboard_port: int = DEFAULT_DASHBOARD_PORT
    edge_port: int = DEFAULT_EDGE_PORT
    ceiling_cm: int = DEFAULT_CEILING_CM
    margin_cm: int = DEFAULT_MARGIN_CM
    geofence_cm: int = DEFAULT_GEOFENCE_CM
    detector_profile: str = "default"
    stream_video: bool = False
    llm: bool = False
    otel_enabled: bool = False
    otel_endpoint: str = DEFAULT_OTEL_ENDPOINT

    # ---- derived views -------------------------------------------------

    @property
    def runs_ground(self) -> bool:
        return self.role == "ground"

    def fleet_ids(self) -> tuple[str, ...]:
        """The ground's ``--fleet`` list: exactly the selected drones, in order."""
        return tuple(d.drone_id for d in self.drones)

    def local_drones(self) -> tuple[DroneSettings, ...]:
        """The drones whose edge runs on this computer."""
        if self.role == "edges":
            return self.drones
        return tuple(d for d in self.drones if d.run_here)

    def edge_bind_hosts(self) -> tuple[str, ...]:
        """Where the ground's edge WebSocket listens: loopback, plus the cable address in the cable layout."""
        if self.network == "cable":
            return ("127.0.0.1", self.cable_address)
        return ("127.0.0.1",)

    def edge_ws_url(self) -> str:
        """The URL a local edge dials: loopback when the ground is here, else the ground host entered."""
        if self.role == "ground":
            return f"ws://127.0.0.1:{self.edge_port}"
        host = self.ground_host.strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"ws://{host}:{self.ground_port}"

    def needs_token(self) -> bool:
        """True when an address other than loopback is bound or dialled, so the edge token is required."""
        if self.role == "ground":
            return self.network == "cable"
        return not _is_loopback(self.ground_host)

    # ---- serialisation -------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["drones"] = [asdict(d) for d in self.drones]
        return data

    @classmethod
    def from_json(cls, data: Any) -> LauncherSettings:
        """Settings from a JSON object (the window or a preset). Unknown keys are ignored.

        Raises ``SettingsError`` when a value has the wrong type.
        """
        if not isinstance(data, Mapping):
            raise SettingsError("settings must be a JSON object")
        defaults = cls()
        raw_drones = data.get("drones", [asdict(d) for d in defaults.drones])
        if not isinstance(raw_drones, list):
            raise SettingsError("drones must be a list")
        drones = tuple(_drone_from_json(i, item) for i, item in enumerate(raw_drones))
        return cls(
            role=_text(data, "role", defaults.role),
            network=_text(data, "network", defaults.network),
            cable_address=_text(data, "cable_address", defaults.cable_address),
            drones=drones,
            ground_host=_text(data, "ground_host", defaults.ground_host),
            ground_port=_int(data, "ground_port", defaults.ground_port),
            dashboard_port=_int(data, "dashboard_port", defaults.dashboard_port),
            edge_port=_int(data, "edge_port", defaults.edge_port),
            ceiling_cm=_int(data, "ceiling_cm", defaults.ceiling_cm),
            margin_cm=_int(data, "margin_cm", defaults.margin_cm),
            geofence_cm=_int(data, "geofence_cm", defaults.geofence_cm),
            detector_profile=_text(data, "detector_profile", defaults.detector_profile),
            stream_video=_bool(data, "stream_video", defaults.stream_video),
            llm=_bool(data, "llm", defaults.llm),
            otel_enabled=_bool(data, "otel_enabled", defaults.otel_enabled),
            otel_endpoint=_text(data, "otel_endpoint", defaults.otel_endpoint),
        )


# ---------------------------------------------------------------------------
# JSON coercion helpers
# ---------------------------------------------------------------------------


def _text(data: Mapping[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SettingsError(f"{key} must be text, got {type(value).__name__}")
    if _CONTROL.search(value):
        raise SettingsError(f"{key} must not contain control characters")
    if len(value) > MAX_TEXT_LEN:
        raise SettingsError(f"{key} is longer than {MAX_TEXT_LEN} characters")
    return value.strip()


def _int(data: Mapping[str, Any], key: str, default: int | None, *, optional: bool = False) -> Any:
    value = data.get(key, default)
    if value is None or (isinstance(value, str) and not value.strip()):
        if optional:
            return None
        raise SettingsError(f"{key} is required")
    if isinstance(value, bool):
        raise SettingsError(f"{key} must be a whole number, got {value!r}")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise SettingsError(f"{key} must be a whole number, got {value!r}")


def _bool(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SettingsError(f"{key} must be true or false, got {value!r}")
    return value


def _drone_from_json(index: int, item: Any) -> DroneSettings:
    if not isinstance(item, Mapping):
        raise SettingsError(f"drone {index + 1} must be a JSON object")
    d = DroneSettings()
    try:
        return DroneSettings(
            drone_id=_text(item, "drone_id", f"drone-{index + 1}"),
            source=_text(item, "source", d.source),
            tello_host=_text(item, "tello_host", d.tello_host),
            local_ip=_text(item, "local_ip", ""),
            ifindex=_int(item, "ifindex", None, optional=True),
            alt_slot_cm=_int(item, "alt_slot_cm", d.alt_slot_cm),
            run_here=_bool(item, "run_here", d.run_here),
        )
    except SettingsError as exc:
        raise SettingsError(f"drone {index + 1}: {exc}") from None


def _is_loopback(host: str) -> bool:
    text = host.strip().strip("[]")
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _ipv4(text: str) -> ipaddress.IPv4Address | None:
    try:
        return ipaddress.IPv4Address(text.strip())
    except ValueError:
        return None


def _valid_hostname(text: str) -> bool:
    host = text.strip()
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    return all(
        0 < len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
        and all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(
    settings: LauncherSettings,
    *,
    is_windows: bool | None = None,
    reserved_ports: Mapping[int, str] | None = None,
    profile_names: tuple[str, ...] | None = None,
    adapters: list[Adapter] | None = None,
    secret: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[Issue]:
    """Every problem with *settings*. Issues of level ``error`` must block a start.

    ``reserved_ports`` maps ports already taken by the launcher itself (its own
    window) to a name. ``profile_names`` defaults to the detection profile registry.
    ``adapters`` (this computer's adapters, when known) lets a real drone with no
    local IP be refused when more than one adapter could reach it. ``secret`` is
    the edge token: a setting that contains it is refused, so it can never reach a
    command line. ``environ`` is the environment the children inherit.
    """
    if is_windows is None:
        is_windows = sys.platform == "win32"
    issues: list[Issue] = []

    def err(field_name: str, message: str) -> None:
        issues.append(Issue(ERROR, field_name, message))

    def warn(field_name: str, message: str) -> None:
        issues.append(Issue(WARNING, field_name, message))

    if secret and secret in json.dumps(settings.to_json()):
        err("settings", "a setting contains the edge token; remove it (the token is passed to the processes for you)")

    if settings.role not in ROLES:
        err("role", f"role must be one of {', '.join(ROLES)}, got {settings.role!r}")
    if settings.network not in NETWORKS:
        err("network", f"network must be one of {', '.join(NETWORKS)}, got {settings.network!r}")

    count = len(settings.drones)
    if not MIN_DRONES <= count <= MAX_DRONES:
        err("drones", f"choose {MIN_DRONES} to {MAX_DRONES} drones, got {count}")

    # Ceiling first: the slots are checked against it.
    ceiling_ok = True
    try:
        validate_ceiling(settings.ceiling_cm, settings.margin_cm)
    except ValueError as exc:
        ceiling_ok = False
        err("ceiling_cm", str(exc))
    if not 0 < settings.geofence_cm <= MAX_GEOFENCE_CM:
        err("geofence_cm", f"geofence must be within 1-{MAX_GEOFENCE_CM} cm, got {settings.geofence_cm}")

    _validate_drones(settings, is_windows, err, warn, ceiling_ok)
    if adapters is not None:
        _validate_shared_subnet(settings, adapters, err)
    _validate_network(settings, err, warn)
    _validate_ports(settings, reserved_ports or {}, err)

    if profile_names is None:
        from flightrisk.vision.profiles import REGISTRY

        profile_names = REGISTRY.names()
    if settings.detector_profile not in profile_names:
        err("detector_profile", f"unknown detector profile {settings.detector_profile!r}; choose from {', '.join(profile_names)}")

    if settings.otel_enabled:
        endpoint = settings.otel_endpoint.strip()
        if not endpoint.startswith(("http://", "https://")):
            err("otel_endpoint", f"the observability endpoint must start with http:// or https://, got {endpoint!r}")

    if settings.role == "ground" and settings.llm:
        env = os.environ if environ is None else environ
        provider = (env.get("FLIGHTRISK_REASONING_PROVIDER") or "").strip().lower()
        if provider == "claude":
            warn("llm", "the language model is on and FLIGHTRISK_REASONING_PROVIDER=claude is inherited: the ground "
                 "station may send match data to the Claude cloud service (if FLIGHTRISK_ALLOW_CLOUD_LLM allows it)")
        else:
            warn("llm", "the language model is on: the ground station connects to the local Ollama server at start")
    return issues


def local_tellos_without_adapter(settings: LauncherSettings, adapters: list[Adapter]) -> list[tuple[int, DroneSettings, list[Adapter]]]:
    """Real drones flown from here with no local IP while more than one adapter can reach their Tello host.

    Such a drone's packets would go out whichever adapter the routing picks, so another drone could answer.
    """
    found = []
    for i, drone in enumerate(settings.drones):
        if drone.source != "tello" or drone.local_ip or not (settings.role == "edges" or drone.run_here):
            continue
        reach = adapters_for_host(adapters, drone.tello_host)
        if len(reach) > 1:
            found.append((i, drone, reach))
    return found


def choose_adapter_message(drone: DroneSettings, reach: list[Adapter]) -> str:
    names = ", ".join(f"{a.name} {a.ipv4}" for a in reach)
    return (f"{drone.drone_id}: choose the adapter; {len(reach)} adapters can reach {drone.tello_host} ({names}), so "
            f"without a local IP another drone could answer")


def _validate_shared_subnet(settings: LauncherSettings, adapters: list[Adapter], err: Callable) -> None:
    for i, drone, reach in local_tellos_without_adapter(settings, adapters):
        err(f"drones.{i}.local_ip", choose_adapter_message(drone, reach))


def _validate_drones(settings: LauncherSettings, is_windows: bool, err: Callable, warn: Callable, ceiling_ok: bool) -> None:
    seen_ids: dict[str, int] = {}
    for i, drone in enumerate(settings.drones):
        name = f"drones.{i}"
        label = drone.drone_id or f"drone {i + 1}"
        if not is_identifier(drone.drone_id) or not _DRONE_ID.match(drone.drone_id):
            err(f"{name}.drone_id", f"drone {i + 1}: {drone.drone_id!r} is not a drone id (1 to 64 letters, digits, '_' "
                f"or '-', starting with a letter or digit, for example drone-1)")
        elif drone.drone_id in seen_ids:
            err(f"{name}.drone_id", f"drone id {drone.drone_id} is used twice (drones {seen_ids[drone.drone_id] + 1} and {i + 1})")
        else:
            seen_ids[drone.drone_id] = i
        if drone.source not in SOURCES:
            err(f"{name}.source", f"{label}: source must be tello or fake, got {drone.source!r}")
        if not MIN_ALT_SLOT_CM <= drone.alt_slot_cm <= MAX_ALT_SLOT_CM:
            err(f"{name}.alt_slot_cm", f"{label}: altitude slot {drone.alt_slot_cm} cm is outside {MIN_ALT_SLOT_CM}-{MAX_ALT_SLOT_CM} cm")
        elif ceiling_ok:
            reason = slot_above_ceiling(drone.alt_slot_cm, settings.ceiling_cm, settings.margin_cm)
            if reason is not None:
                err(f"{name}.alt_slot_cm", f"{label}: {reason}; the edge would refuse to start")
        runs_here = settings.role == "edges" or drone.run_here
        if drone.source == "tello":
            if _ipv4(drone.tello_host) is None:
                err(f"{name}.tello_host", f"{label}: Tello host must be an IPv4 address, got {drone.tello_host!r}")
            if drone.local_ip:
                address = _ipv4(drone.local_ip)
                if address is None or address.is_unspecified or address.is_multicast or address.is_loopback:
                    err(f"{name}.local_ip", f"{label}: local IP must be the IPv4 address of one Wi-Fi adapter, got {drone.local_ip!r}")
            if drone.ifindex is not None:
                if not drone.local_ip:
                    err(f"{name}.ifindex", f"{label}: an interface index needs the local IP of the same adapter")
                elif drone.ifindex <= 0:
                    err(f"{name}.ifindex", f"{label}: interface index must be a positive number, got {drone.ifindex}")
                elif runs_here and not is_windows:
                    err(f"{name}.ifindex", f"{label}: an interface index is only supported on Windows; leave it empty here")
        elif drone.local_ip or drone.ifindex is not None:
            warn(f"{name}.local_ip", f"{label}: a fake drone uses no adapter, so its local IP and interface index are ignored")

    _validate_adapters(settings, err)

    if settings.role == "ground" and settings.network == "loopback":
        for i, drone in enumerate(settings.drones):
            if not drone.run_here:
                err(f"drones.{i}.run_here", f"{drone.drone_id}: its edge runs on another computer, but the ground station "
                    f"listens on loopback only; choose the cable layout (layout A) or run the edge here")


def _validate_adapters(settings: LauncherSettings, err: Callable) -> None:
    """Two real Tellos flown from this computer need one adapter each (docs/two-tellos-one-pc.md)."""
    local_tellos = [(i, d) for i, d in enumerate(settings.drones)
                    if d.source == "tello" and (settings.role == "edges" or d.run_here)]
    if len(local_tellos) < 2:
        return
    by_ip: dict[str, list[str]] = {}
    by_index: dict[int, list[str]] = {}
    for i, drone in local_tellos:
        if not drone.local_ip:
            err(f"drones.{i}.local_ip", f"{drone.drone_id}: {len(local_tellos)} real Tellos run on this computer, so each "
                f"needs the local IP of its own Wi-Fi adapter")
            continue
        by_ip.setdefault(drone.local_ip, []).append(drone.drone_id)
        if drone.ifindex is not None:
            by_index.setdefault(drone.ifindex, []).append(drone.drone_id)
    for ip, ids in by_ip.items():
        if len(ids) > 1:
            for i, drone in local_tellos:
                if drone.local_ip == ip:
                    err(f"drones.{i}.local_ip", f"{' and '.join(ids)} would share local IP {ip}; each drone needs its own adapter")
    for index, ids in by_index.items():
        if len(ids) > 1:
            for i, drone in local_tellos:
                if drone.ifindex == index:
                    err(f"drones.{i}.ifindex", f"{' and '.join(ids)} would share interface index {index}; each adapter has its own index")


def _validate_network(settings: LauncherSettings, err: Callable, warn: Callable) -> None:
    if settings.role == "ground" and settings.network == "cable":
        address = _ipv4(settings.cable_address)
        if address is None or address.is_unspecified or address.is_loopback or address.is_multicast:
            err("cable_address", f"the cable address must be this computer's IPv4 address on the cable, got {settings.cable_address!r}")
    if settings.role == "edges":
        if not _valid_hostname(settings.ground_host):
            err("ground_host", f"enter the ground station's address, for example 10.42.0.1, got {settings.ground_host!r}")
        elif settings.ground_host.strip() in ("0.0.0.0", "::"):
            err("ground_host", "the ground host cannot be a wildcard address")


def _validate_ports(settings: LauncherSettings, reserved: Mapping[int, str], err: Callable) -> None:
    if settings.role == "ground":
        ports = {"dashboard_port": settings.dashboard_port, "edge_port": settings.edge_port}
    else:
        ports = {"ground_port": settings.ground_port}
    for name, port in ports.items():
        if not MIN_PORT <= port <= MAX_PORT:
            err(name, f"{name.replace('_', ' ')} must be within {MIN_PORT}-{MAX_PORT}, got {port}")
        elif settings.role == "ground" and port in reserved:
            err(name, f"{name.replace('_', ' ')} {port} is already used by {reserved[port]}")
    if settings.role == "ground" and settings.dashboard_port == settings.edge_port:
        err("edge_port", f"the dashboard and the edge link cannot share port {settings.edge_port}")


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.level == ERROR]


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


def _two(source: str, *, run_here: bool, slots: tuple[int, int] = DEFAULT_SLOTS_CM, local_ips: tuple[str, str] = ("", "")) -> tuple[DroneSettings, ...]:
    return tuple(
        DroneSettings(drone_id=f"drone-{k + 1}", source=source, alt_slot_cm=slots[k], run_here=run_here, local_ip=local_ips[k])
        for k in range(2)
    )


def builtin_presets() -> dict[str, LauncherSettings]:
    """The presets every launcher has. They cannot be overwritten or deleted."""
    return {
        "Layout A (ground here)": LauncherSettings(
            role="ground", network="cable", drones=_two("tello", run_here=False),
        ),
        "Layout A (edges on this computer)": LauncherSettings(
            role="edges", ground_host=DEFAULT_CABLE_ADDRESS, ground_port=DEFAULT_EDGE_PORT,
            # The same slots as scripts/demo_edge_pair.ps1; on Windows also fill in each adapter's interface index.
            # The adapter IPs of the two-adapter runbook (docs/two-tellos-one-pc.md); check yours with Refresh adapters.
            drones=_two("tello", run_here=True, slots=(50, 80), local_ips=("192.168.10.2", "192.168.10.50")),
        ),
        "Layout B (everything here)": LauncherSettings(
            # Two real Tellos from one computer need one Wi-Fi adapter each; these are the runbook's IPs.
            role="ground", network="loopback",
            drones=_two("tello", run_here=True, local_ips=("192.168.10.2", "192.168.10.50")),
        ),
        "Single drone": LauncherSettings(
            role="ground", network="loopback", drones=(DroneSettings(drone_id="drone-1", source="tello"),),
        ),
        "Rehearsal with fake drones": LauncherSettings(
            # Other ports than the live ground station's, so a rehearsal can run beside it.
            role="ground", network="loopback", drones=_two("fake", run_here=True),
            dashboard_port=5800, edge_port=9800, geofence_cm=500,
        ),
    }


DEFAULT_PRESET = "Rehearsal with fake drones"


def default_launcher_file(environ: Mapping[str, str] | None = None) -> Path:
    """``FLIGHTRISK_LAUNCHER_FILE``, else ``~/.flightrisk/launcher.json``."""
    env = os.environ if environ is None else environ
    raw = (env.get(LAUNCHER_FILE_ENV) or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".flightrisk" / "launcher.json"


class PresetError(ValueError):
    """A preset cannot be saved or deleted; the message says why."""


class PresetStore:
    """Saved presets and the last used settings, as JSON in the user's home directory.

    The file holds ``{"version": 1, "presets": {name: settings}, "last": settings}``.
    An entry that is not valid is skipped and reported in ``load_problems``.
    ``secret`` is a callable returning the edge token (or None); a save whose
    JSON would contain it is refused, so a preset can never hold the token.
    """

    def __init__(self, path: str | os.PathLike | None = None, *, secret: Callable[[], str | None] = lambda: None):
        self.path = Path(path).expanduser() if path is not None else default_launcher_file()
        self._secret = secret
        self._lock = threading.Lock()
        self.load_problems: list[str] = []

    def _read(self) -> dict[str, Any]:
        self.load_problems = []
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": _FILE_VERSION, "presets": {}, "last": None}
        except OSError as exc:
            self.load_problems.append(f"cannot read {self.path}: {exc}")
            return {"version": _FILE_VERSION, "presets": {}, "last": None}
        try:
            data = json.loads(text)
        except ValueError as exc:
            self.load_problems.append(f"{self.path} is not valid JSON ({exc}); it will be replaced on the next save")
            return {"version": _FILE_VERSION, "presets": {}, "last": None}
        if not isinstance(data, dict) or data.get("version") != _FILE_VERSION:
            self.load_problems.append(f"{self.path} is not a version {_FILE_VERSION} launcher file; it will be replaced on the next save")
            return {"version": _FILE_VERSION, "presets": {}, "last": None}
        presets = data.get("presets") if isinstance(data.get("presets"), dict) else {}
        return {"version": _FILE_VERSION, "presets": presets, "last": data.get("last")}

    def presets(self) -> dict[str, LauncherSettings]:
        """Built-in presets first, then the saved ones that are valid."""
        with self._lock:
            raw = self._read()
        result = dict(builtin_presets())
        for name, value in raw["presets"].items():
            if name in result:
                continue
            try:
                result[name] = LauncherSettings.from_json(value)
            except SettingsError as exc:
                self.load_problems.append(f"preset {name!r} skipped: {exc}")
        return result

    def last(self) -> LauncherSettings | None:
        with self._lock:
            raw = self._read()
        if raw["last"] is None:
            return None
        try:
            return LauncherSettings.from_json(raw["last"])
        except SettingsError:
            return None

    def save_preset(self, name: str, settings: LauncherSettings) -> None:
        name = (name or "").strip()
        if not name or len(name) > MAX_PRESET_NAME or not name.isprintable():
            raise PresetError(f"a preset name is 1 to {MAX_PRESET_NAME} printable characters")
        if name in builtin_presets():
            raise PresetError(f"{name!r} is a built-in preset; save under another name")
        with self._lock:
            raw = self._read()
            raw["presets"][name] = settings.to_json()
            self._write(raw)

    def delete_preset(self, name: str) -> None:
        if name in builtin_presets():
            raise PresetError(f"{name!r} is a built-in preset and cannot be deleted")
        with self._lock:
            raw = self._read()
            if name not in raw["presets"]:
                raise PresetError(f"no saved preset named {name!r}")
            del raw["presets"][name]
            self._write(raw)

    def save_last(self, settings: LauncherSettings) -> None:
        with self._lock:
            raw = self._read()
            raw["last"] = settings.to_json()
            self._write(raw)

    def _write(self, raw: dict[str, Any]) -> None:
        text = json.dumps(raw, indent=2, sort_keys=True) + "\n"
        secret = self._secret()
        if secret and secret in text:
            raise PresetError("refused to save: the settings contain the edge token")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

