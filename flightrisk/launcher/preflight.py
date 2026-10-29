"""Read-only checks before a start: environment, weights, ports, token, adapters and drones.

Every check returns a ``Check`` with status ``PASS``, ``WARN``, ``FAIL`` or
``INFO``; any ``FAIL`` blocks the start. Nothing here changes the computer or a
drone. The Tello probe sends only ``command`` (enter SDK mode), ``battery?``
and ``temp?``; it can never send a takeoff, move, land or motor command
(``probe_tello`` refuses any other text before it reaches a socket).

All side effects go through ``PreflightEnv`` so tests can inject fakes.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flightrisk.launcher.commands import EdgeToken
from flightrisk.launcher.netinfo import Adapter, address_owners, list_adapters
from flightrisk.launcher.settings import (
    UNREADABLE_ADAPTERS,
    DroneSettings,
    LauncherSettings,
    choose_adapter_message,
    local_tellos_with_unknown_adapter,
    local_tellos_without_adapter,
)

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
INFO = "INFO"

TELLO_CMD_PORT = 8889
#: The only text the probe may send to a drone. Each is a query or the SDK-mode handshake.
PROBE_COMMANDS = ("command", "battery?", "temp?")
PROBE_TIMEOUT_S = 2.5
#: Edges refuse a takeoff below this battery percentage (FLIGHTRISK_MIN_TAKEOFF_BATTERY, default 30).
MIN_TAKEOFF_BATTERY = 30
#: Windows IP_UNICAST_IF (not named in Python's socket module): pins outgoing packets to an interface index.
IP_UNICAST_IF = 31

GROUND_MODULES = ("flask", "flask_socketio", "websockets", "numpy", "cv2")
EDGE_MODULES = ("websockets", "numpy", "cv2", "ultralytics")
TELLO_MODULES = ("djitellopy",)


@dataclass(frozen=True)
class Check:
    """One preflight row."""

    name: str
    status: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "data": self.data}


@dataclass(frozen=True)
class TelloProbe:
    """What a Tello answered. ``battery`` is a percentage, ``temp`` the drone's text (for example 60~62C)."""

    ok: bool
    battery: int | None = None
    temp: str | None = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Tello probe (read-only)
# ---------------------------------------------------------------------------


def _is_text(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return all(ch.isprintable() or ch in "\r\n\t\x00" for ch in text)


def _ask(sock: Any, host: str, command: str, timeout: float) -> str | None:
    """Send one probe command and return the reply text, or None on timeout."""
    if command not in PROBE_COMMANDS:
        raise ValueError(f"the launcher probe never sends {command!r}")
    # Drop late replies to an earlier command so they are not read as this one's answer.
    sock.settimeout(0.0)
    try:
        while True:
            sock.recvfrom(2048)
    except (TimeoutError, BlockingIOError, OSError):
        pass
    sock.sendto(command.encode("utf-8"), (host, TELLO_CMD_PORT))
    wants_value = command.endswith("?")
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(2048)
        except TimeoutError:
            return None
        if not _is_text(data):
            continue
        text = data.decode("utf-8").strip().strip("\x00").strip()
        if wants_value and text.lower() == "ok":
            continue  # a duplicate acknowledgement of "command", not the value asked for
        return text


def probe_tello(
    host: str,
    local_ip: str = "",
    ifindex: int | None = None,
    *,
    socket_factory: Callable[[], Any] | None = None,
    timeout: float = PROBE_TIMEOUT_S,
    is_windows: bool | None = None,
) -> TelloProbe:
    """Ask one Tello for its battery and temperature, through the adapter with *local_ip* when given.

    Uses one UDP socket for all three commands (a Tello answers the port that
    sent ``command``). The socket binds local port 8889 like the edge does,
    else any free port, on *local_ip* (never the wildcard address when a local
    IP is given), and is closed before returning, so an edge started next can bind it.
    """
    if is_windows is None:
        is_windows = sys.platform == "win32"
    factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
    try:
        sock = factory()
    except OSError as exc:
        return TelloProbe(False, detail=f"cannot open a UDP socket: {exc}")
    try:
        if ifindex is not None and is_windows:
            sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, int(ifindex).to_bytes(4, "big"))
        bind_ip = local_ip or ""
        try:
            sock.bind((bind_ip, TELLO_CMD_PORT))
        except OSError:
            try:
                sock.bind((bind_ip, 0))
            except OSError as exc:
                return TelloProbe(False, detail=f"cannot bind to local IP {local_ip}: {exc}")
        via = f" via {local_ip}" if local_ip else ""
        reply = _ask(sock, host, "command", timeout)
        if reply is None:
            return TelloProbe(False, detail=f"no reply from {host}{via}; is this computer joined to the drone's Wi-Fi and the drone on?")
        if reply.lower() != "ok":
            return TelloProbe(False, detail=f"{host}{via} answered {reply!r} instead of ok")
        battery_text = _ask(sock, host, "battery?", timeout)
        temp = _ask(sock, host, "temp?", timeout)
        battery = None
        if battery_text is not None:
            try:
                value = int(battery_text)
                battery = value if 0 <= value <= 100 else None
            except ValueError:
                battery = None
        if battery is None:
            return TelloProbe(False, temp=temp, detail=f"{host}{via} gave no battery reading ({battery_text!r})")
        return TelloProbe(True, battery=battery, temp=temp, detail=f"{host}{via}")
    except OSError as exc:
        return TelloProbe(False, detail=f"network error talking to {host}: {exc}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Environment for the checks (injectable)
# ---------------------------------------------------------------------------


def _port_free(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _in_venv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _module_found(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ceiling_file() -> tuple[int, int, str | None, str]:
    """(ceiling, margin, error, path) from the dashboard's ceiling settings file."""
    from flightrisk.dashboard.ceiling import CeilingStore

    store = CeilingStore()
    settings, error = store.load()
    return settings.ceiling_cm, settings.margin_cm, error, str(store.path)


def _weights() -> list[dict[str, Any]]:
    """Each detection profile with its effective weights, whether they are required, and the file found."""
    from flightrisk.vision.profiles import REGISTRY, resolve_weights

    rows = []
    for profile in REGISTRY:
        weights = profile.effective_weights()
        rows.append({
            "name": profile.name,
            "label": profile.label,
            "weights": weights,
            "required": profile.weights_required,
            "found": resolve_weights(weights),
            "fallback": profile.fallback,
        })
    return rows


@dataclass
class PreflightEnv:
    """The side effects preflight needs. Replace any of them in tests."""

    in_venv: Callable[[], bool] = _in_venv
    python: str = sys.executable
    module_found: Callable[[str], bool] = _module_found
    port_free: Callable[[str, int], bool] = _port_free
    tcp_reachable: Callable[[str, int], bool] = _tcp_reachable
    adapters: Callable[[], list[Adapter]] = list_adapters
    weights: Callable[[], list[dict[str, Any]]] = _weights
    ceiling_file: Callable[[], tuple[int, int, str | None, str]] = _ceiling_file
    tello_probe: Callable[[DroneSettings], TelloProbe] = field(
        default=lambda d: probe_tello(d.tello_host, d.local_ip, d.ifindex)
    )
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    is_windows: bool = sys.platform == "win32"


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def run_preflight(
    settings: LauncherSettings,
    token: EdgeToken,
    env: PreflightEnv | None = None,
    *,
    probe_drones: bool = True,
) -> list[Check]:
    """Every preflight row for *settings*. Any ``FAIL`` must block the start."""
    env = env or PreflightEnv()
    checks: list[Check] = []
    local = settings.local_drones()
    local_tellos = [d for d in local if d.source == "tello"]

    # Python and packages.
    venv_ok = env.in_venv()
    checks.append(Check(
        "Virtual environment", PASS if venv_ok else WARN,
        f"{env.python}" if venv_ok else f"{env.python} is not a virtual environment; start the launcher with the "
        f"repository's venv (scripts/launcher.command or scripts\\launcher.bat)",
    ))
    wanted: list[str] = []
    if settings.runs_ground:
        wanted += GROUND_MODULES
    if local:
        wanted += EDGE_MODULES
    if local_tellos:
        wanted += TELLO_MODULES
    wanted = list(dict.fromkeys(wanted))
    missing = [m for m in wanted if not env.module_found(m)]
    checks.append(Check(
        "Dependencies", FAIL if missing else PASS,
        f"missing: {', '.join(missing)} (pip install -r requirements.txt)" if missing else f"{len(wanted)} packages found",
    ))

    # Detector weights for the chosen profile, when an edge runs here.
    if local:
        checks.append(_weights_check(settings, env))

    # Ports and the cable address.
    adapters = env.adapters()
    if settings.runs_ground:
        for label, port in (("Dashboard port", settings.dashboard_port), ("Edge link port", settings.edge_port)):
            free = env.port_free("127.0.0.1", port)
            checks.append(Check(
                f"{label} {port}", PASS if free else FAIL,
                "free on 127.0.0.1" if free else "in use on 127.0.0.1 by another program; pick another port or stop that "
                "program yourself (the launcher only stops what it started)",
            ))
        if settings.network == "cable":
            owners = address_owners(adapters, settings.cable_address)
            if not owners:
                checks.append(Check(
                    f"Cable address {settings.cable_address}", FAIL,
                    "not assigned to any adapter on this computer; connect the cable and set the address "
                    "(docs/fleet-setup.md, Path 2 or 3)",
                ))
            else:
                free = env.port_free(settings.cable_address, settings.edge_port)
                checks.append(Check(
                    f"Cable address {settings.cable_address}", PASS if free else FAIL,
                    f"on {owners[0].name}; port {settings.edge_port} free" if free
                    else f"port {settings.edge_port} is in use on {settings.cable_address}",
                ))
    else:
        reachable = env.tcp_reachable(settings.ground_host, settings.ground_port)
        checks.append(Check(
            f"Ground station {settings.ground_host}:{settings.ground_port}", PASS if reachable else WARN,
            "listening" if reachable else "nothing answers yet; the edges keep retrying, but start the ground station "
            "first and check the cable",
        ))

    # Token: only its status, never its value.
    needed = settings.needs_token()
    if token.status == "set":
        checks.append(Check("Edge token", PASS, f"set (from {token.source})"))
    elif needed:
        checks.append(Check(
            "Edge token", FAIL,
            "missing; a non-loopback link needs it. Set FLIGHTRISK_EDGE_TOKEN or add it to ~/.flightrisk-fleet.env "
            "(the same value on every computer)",
        ))
    else:
        checks.append(Check("Edge token", INFO, "missing; not needed while everything stays on loopback"))

    # Adapters and the local IP of each real drone.
    shown = [a for a in adapters if not a.loopback]
    checks.append(Check(
        "Network adapters", INFO if adapters else WARN,
        ", ".join(f"{a.name} {a.ipv4}" + (f" (index {a.index})" if a.index is not None else "") for a in shown)
        or ("only loopback" if adapters else "could not list the adapters"),
        {"adapters": [a.to_json() for a in adapters]},
    ))
    for drone in local_tellos:
        if drone.local_ip:
            checks.append(_local_ip_check(drone, adapters, env.is_windows))
    ambiguous = {drone.drone_id for _, drone, _ in local_tellos_without_adapter(settings, adapters)}
    for _, drone, reach in local_tellos_without_adapter(settings, adapters):
        checks.append(Check(f"Adapter for {drone.drone_id}", FAIL, choose_adapter_message(drone, reach)))
    for _, drone in local_tellos_with_unknown_adapter(settings, adapters):
        # Fail closed: with no adapter list, a probe from all addresses could reach another drone.
        ambiguous.add(drone.drone_id)
        checks.append(Check(f"Adapter for {drone.drone_id}", FAIL, UNREADABLE_ADAPTERS))

    # Ceiling: the dashboard's saved settings are what the ground enforces.
    checks.append(_ceiling_check(settings, env))

    # Each real drone, read-only.
    if probe_drones:
        for drone in local_tellos:
            if drone.drone_id in ambiguous:
                # A probe from all addresses could be answered by another drone; it is not sent.
                checks.append(Check(f"Drone {drone.drone_id}", FAIL, "not checked: choose its adapter first"))
                continue
            result = env.tello_probe(drone)
            if not result.ok:
                checks.append(Check(f"Drone {drone.drone_id}", FAIL, result.detail))
                continue
            low = result.battery is not None and result.battery < MIN_TAKEOFF_BATTERY
            temp = f", temperature {result.temp}" if result.temp else ""
            checks.append(Check(
                f"Drone {drone.drone_id}", WARN if low else PASS,
                f"battery {result.battery}%{temp} ({result.detail})"
                + (f"; below the {MIN_TAKEOFF_BATTERY}% takeoff minimum" if low else ""),
                {"battery": result.battery, "temp": result.temp},
            ))
    elif local_tellos:
        checks.append(Check("Drones", INFO, "not checked; press Check drones"))
    return checks


def _weights_check(settings: LauncherSettings, env: PreflightEnv) -> Check:
    rows = {row["name"]: row for row in env.weights()}
    row = rows.get(settings.detector_profile)
    name = f"Detector weights ({settings.detector_profile})"
    if row is None:
        return Check(name, FAIL, f"unknown profile {settings.detector_profile!r}")
    override = ""
    if settings.detector_profile == "default" and env.environ.get("FLIGHTRISK_DETECTOR_MODEL", "").strip():
        override = " (from FLIGHTRISK_DETECTOR_MODEL)"
    if row["found"]:
        return Check(name, PASS, f"{row['weights']}{override} found")
    if row["required"]:
        return Check(name, FAIL, f"{row['weights']} is missing; the edge would fall back to {row['fallback'] or 'nothing'}. "
                     f"Choose another profile or copy the weights into models/")
    return Check(name, WARN, f"{row['weights']}{override} is not on disk; the first start downloads it, which needs the internet")


def _local_ip_check(drone: DroneSettings, adapters: list[Adapter], is_windows: bool) -> Check:
    name = f"Adapter for {drone.drone_id}"
    owners = address_owners(adapters, drone.local_ip)
    if not owners:
        return Check(name, FAIL, f"{drone.local_ip} is not on any adapter; join the drone's Wi-Fi with that adapter")
    if len(owners) > 1:
        return Check(name, FAIL, f"{drone.local_ip} is on more than one adapter; give each adapter its own IP")
    owner = owners[0]
    if drone.ifindex is not None and owner.index is not None and owner.index != drone.ifindex:
        return Check(name, FAIL, f"{drone.local_ip} is on interface index {owner.index}, not {drone.ifindex}")
    return Check(name, PASS, f"{drone.local_ip} on {owner.name}" + (f" (index {owner.index})" if owner.index is not None else ""))


def _ceiling_check(settings: LauncherSettings, env: PreflightEnv) -> Check:
    if not settings.runs_ground:
        return Check(
            "Ceiling", INFO,
            f"edges start with ceiling {settings.ceiling_cm} cm and margin {settings.margin_cm} cm; the ground's "
            f"dashboard sends its own settings once they connect",
        )
    try:
        ceiling, margin, error, path = env.ceiling_file()
    except Exception as exc:  # noqa: BLE001 - a broken settings file must not stop the other checks
        return Check("Ceiling", WARN, f"could not read the dashboard's ceiling settings: {exc}")
    if error:
        return Check("Ceiling", WARN,
                     f"{error}; the dashboard refuses Plan and Start until valid settings are saved in its Controls tab")
    if (ceiling, margin) != (settings.ceiling_cm, settings.margin_cm):
        return Check(
            "Ceiling", WARN,
            f"the dashboard's saved ceiling is {ceiling} cm with a {margin} cm margin ({Path(path).name}), the launcher's "
            f"is {settings.ceiling_cm} cm with {settings.margin_cm} cm. Edges start with the launcher's values and the "
            f"dashboard then sends its own; change the dashboard's in its Controls tab",
        )
    return Check("Ceiling", PASS, f"{ceiling} cm, margin {margin} cm, slots up to {ceiling - margin} cm")
