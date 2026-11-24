"""Test support for the opt-in SITL tests: environment, readiness probe and mavsdk_server housekeeping.

Simulation only. The SITL tests start MAVSDK servers of their own, and a server that outlives its test keeps the
vehicle's single TCP slot (ArduCopter) or its UDP port (PX4), so every later connect in the same pytest process
times out. This module holds what the integration ``conftest.py``, the tests and the fleet harness share:

* ``sitl_env()`` reads the variables the harness exports.
* ``ready_problems()`` and ``wait_ready()`` decide, with the product's own link, whether a vehicle may be flown.
* ``child_mavsdk_servers()`` and ``reap_child_mavsdk_servers()`` stop the servers this process left behind.
* ``foreign_mavsdk_servers()`` reports servers that other processes started for the same vehicles.

The harness calls ``python -m tests.integration.sitl_support wait-ready --timeout S --address A [--address B ...]``.

``mavsdk`` and ``flightrisk.drone`` are imported only inside functions, so collecting this module never binds a
fake ``mavsdk`` package and the unit tests need no MAVSDK.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

ENV_ADDRESS = "FLIGHTRISK_SITL_ADDRESS"
ENV_ADDRESS_2 = "FLIGHTRISK_SITL_ADDRESS_2"
ENV_ADDRESSES = "FLIGHTRISK_SITL_ADDRESSES"
ENV_SYSIDS = "FLIGHTRISK_SITL_SYSIDS"
ENV_AUTOPILOT = "FLIGHTRISK_SITL_AUTOPILOT"
ENV_VERSION = "FLIGHTRISK_SITL_VERSION"
ENV_READY_TIMEOUT = "FLIGHTRISK_SITL_READY_TIMEOUT_S"

#: Under amd64 emulation the EKF can take minutes to settle (the value test_sitl_fleet.py has always used).
DEFAULT_READY_TIMEOUT_S = 300.0
#: How often wait_ready() looks at the vehicle's state.
POLL_INTERVAL_S = 1.0
#: How long a terminated mavsdk_server gets before it is killed.
TERMINATE_GRACE_S = 5.0
#: Pause after a TCP vehicle's client went away; a TCP port cannot be probed without taking its one client slot.
TCP_RELEASE_PAUSE_S = 2.0

#: The values of ``flightrisk.drone.autopilot.ARDUPILOT`` and ``PX4``, repeated here to keep imports lazy.
ARDUPILOT = "ardupilot"
PX4 = "px4"
_AUTOPILOTS = (ARDUPILOT, PX4)
#: System id parameters in the order read, per autopilot; an unknown autopilot tries all of them.
_SYSID_PARAMS = {
    ARDUPILOT: ("MAV_SYSID", "SYSID_THISMAV"),
    PX4: ("MAV_SYS_ID",),
}
_MIN_GPS_FIX = 3


class SitlNotReady(RuntimeError):
    """A vehicle did not become ready in time; the message names the last problems seen."""


@dataclass(frozen=True)
class SitlEnv:
    """The SITL fleet as the harness described it. ``autopilot`` is None when nothing declared it."""

    addresses: tuple[str, ...] = ()
    autopilot: str | None = None
    version: str | None = None
    sysids: tuple[int, ...] = ()
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S


@dataclass(frozen=True)
class ReadyReport:
    """A vehicle that passed ``ready_problems``: its address, autopilot, system id (None if unreadable) and wait."""

    address: str
    autopilot: str | None
    sysid: int | None
    waited_s: float


def _split(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def sitl_env(environ: Mapping[str, str] | None = None) -> SitlEnv:
    """Read the harness exports; fall back to ``FLIGHTRISK_SITL_ADDRESS`` and ``_2``.

    The system ids default to 1 per address. Raises ``ValueError`` for a malformed value or when the number of
    system ids differs from the number of addresses.
    """
    env = os.environ if environ is None else environ
    addresses = _split(env.get(ENV_ADDRESSES, ""))
    if not addresses:
        addresses = [a for a in (env.get(ENV_ADDRESS, "").strip(), env.get(ENV_ADDRESS_2, "").strip()) if a]
    sysid_text = _split(env.get(ENV_SYSIDS, ""))
    try:
        sysids = [int(part) for part in sysid_text]
    except ValueError as exc:
        raise ValueError(f"{ENV_SYSIDS} must be a comma list of integers, got {env.get(ENV_SYSIDS)!r}") from exc
    if not sysids:
        sysids = [1] * len(addresses)
    elif len(sysids) != len(addresses):
        raise ValueError(
            f"{ENV_SYSIDS} lists {len(sysids)} system ids but there are {len(addresses)} addresses"
        )
    autopilot = env.get(ENV_AUTOPILOT, "").strip().lower() or None
    if autopilot is not None and autopilot not in _AUTOPILOTS:
        raise ValueError(f"{ENV_AUTOPILOT} must be one of {_AUTOPILOTS}, got {autopilot!r}")
    timeout_text = env.get(ENV_READY_TIMEOUT, "").strip()
    try:
        timeout_s = float(timeout_text) if timeout_text else DEFAULT_READY_TIMEOUT_S
    except ValueError as exc:
        raise ValueError(f"{ENV_READY_TIMEOUT} must be a number, got {timeout_text!r}") from exc
    return SitlEnv(
        addresses=tuple(addresses),
        autopilot=autopilot,
        version=env.get(ENV_VERSION, "").strip() or None,
        sysids=tuple(sysids),
        ready_timeout_s=timeout_s,
    )


def sysid_for(address: str, environ: Mapping[str, str] | None = None) -> int:
    """The system id the harness gave the vehicle at *address*; 1 when it is not listed."""
    env = sitl_env(environ)
    if address in env.addresses:
        return env.sysids[env.addresses.index(address)]
    return 1


def ready_problems(state: Any) -> list[str]:
    """Why the vehicle may not be flown yet; an empty list means ready. Pure.

    Home health is the EKF proxy: ArduCopter sets its home only once the EKF has an absolute position, and the
    fleet preflight already relies on that flag. Takeoff before that is refused ("requires position").
    """
    problems: list[str] = []
    if not getattr(state, "connected", False):
        problems.append("not connected")
    fix = getattr(state, "gps_fix_type", None)
    if fix is None or fix < _MIN_GPS_FIX:
        problems.append(f"GPS fix type is {fix}, need {_MIN_GPS_FIX} (3D) or better")
    for flag, label in (
        ("global_position_ok", "global position is not ok"),
        ("home_position_ok", "home position is not ok (EKF has no absolute position yet)"),
        ("armable", "vehicle is not armable"),
    ):
        if getattr(state, flag, None) is not True:
            problems.append(label)
    if getattr(state, "armed", None) is True:
        problems.append("vehicle is armed")
    if getattr(state, "in_air", None) is True:
        problems.append("vehicle is in the air")
    return problems


def _default_link_factory(address: str, timeout_s: float) -> Any:
    from flightrisk.drone.mavsdk_link import MavsdkAutopilotLink

    return MavsdkAutopilotLink(address, connect_timeout_s=max(1.0, timeout_s))


def _read_sysid(link: Any, autopilot: str | None) -> int | None:
    names = _SYSID_PARAMS.get(autopilot or "") or tuple(n for p in _AUTOPILOTS for n in _SYSID_PARAMS[p])
    for name in names:
        try:
            value = link.get_param(name)
        except Exception:  # noqa: BLE001 - an unreadable parameter is "unknown", not a failure
            continue
        if value is not None:
            return int(value)
    return None


def wait_ready(
    address: str,
    timeout_s: float,
    link_factory: Callable[[str], Any] | None = None,
    *,
    poll_s: float = POLL_INTERVAL_S,
) -> ReadyReport:
    """Connect to *address*, poll ``ready_problems`` until it is empty, and report; always disconnect.

    *link_factory* takes the address and returns a link with ``connect``, ``disconnect``, ``state`` and
    ``get_param`` (the default is a ``MavsdkAutopilotLink`` with its own supervised ``mavsdk_server``).
    Raises ``SitlNotReady`` naming the last problems when *timeout_s* passes first.
    """
    started = time.monotonic()
    link = link_factory(address) if link_factory is not None else _default_link_factory(address, timeout_s)
    try:
        try:
            link.connect()
        except Exception as exc:  # noqa: BLE001 - reported with the address
            raise SitlNotReady(f"{address}: could not connect: {exc}") from exc
        problems: list[str] = ["no state read yet"]
        state = None
        while True:
            state = link.state()
            problems = ready_problems(state)
            if not problems:
                break
            if time.monotonic() - started >= timeout_s:
                raise SitlNotReady(f"{address}: not ready after {timeout_s:g} s: " + "; ".join(problems))
            time.sleep(poll_s)
        autopilot = getattr(state, "autopilot", None)
        if autopilot is None and hasattr(link, "refresh_autopilot"):
            autopilot = link.refresh_autopilot()
        return ReadyReport(address, autopilot, _read_sysid(link, autopilot), time.monotonic() - started)
    finally:
        with contextlib.suppress(Exception):
            link.disconnect()


# ---------------------------------------------------------------------------
# Autopilot selection for tests
# ---------------------------------------------------------------------------

_probed: dict[str, str | None] = {}


def resolve_autopilot(env: SitlEnv | None = None) -> str | None:
    """The declared autopilot, else the first vehicle's, probed once per process. None without an address."""
    env = sitl_env() if env is None else env
    if env.autopilot is not None:
        return env.autopilot
    if not env.addresses:
        return None
    address = env.addresses[0]
    if address not in _probed:
        _probed[address] = wait_ready(address, env.ready_timeout_s).autopilot
    return _probed[address]


def require_autopilot(name: str) -> None:
    """Skip the calling test unless the declared or probed autopilot is *name*. Call before any connect."""
    import pytest

    env = sitl_env()
    if not env.addresses:
        pytest.skip(f"set {ENV_ADDRESS} to a running SITL (docs/sitl-demo.md)")
    try:
        actual = resolve_autopilot(env)
    except SitlNotReady as exc:
        pytest.fail(f"could not tell which autopilot the SITL runs: {exc}")
    if actual != name:
        pytest.skip(f"needs {name}; the SITL runs {actual or 'an unknown autopilot'}")


# ---------------------------------------------------------------------------
# mavsdk_server housekeeping
# ---------------------------------------------------------------------------


def _is_windows() -> bool:
    return sys.platform == "win32"


def _run_text(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout or ""


def child_mavsdk_servers() -> list[int]:
    """Pids of this process's ``mavsdk_server`` children; empty on Windows."""
    if _is_windows():
        return []
    out = _run_text(["pgrep", "-P", str(os.getpid()), "-x", "mavsdk_server"])
    return [int(token) for token in out.split() if token.isdigit()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A terminated child that was not waited for is a zombie; it no longer holds the vehicle's port.
    state = _run_text(["ps", "-o", "state=", "-p", str(pid)]).strip()
    return bool(state) and not state.startswith("Z")


def reap_child_mavsdk_servers() -> list[int]:
    """SIGTERM this process's ``mavsdk_server`` children, then SIGKILL the ones alive after 5 s. Returns the pids."""
    pids = child_mavsdk_servers()
    if not pids:
        return []
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_S
    while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.1)
    for pid in pids:
        if _alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    return pids


def _port_of(address: str) -> str | None:
    match = re.search(r":(\d+)$", address.strip())
    return match.group(1) if match else None


def foreign_mavsdk_servers(addresses: tuple[str, ...] | list[str]) -> list[tuple[int, str]]:
    """``(pid, command line)`` of other processes' ``mavsdk_server`` that name one of *addresses*.

    These are reported, never killed: they belong to someone else. A command line names an address when it holds
    the address itself or its port after a colon.
    """
    if _is_windows() or not addresses:
        return []
    found: list[tuple[int, str]] = []
    me = os.getpid()
    for line in _run_text(["ps", "-axo", "pid=,ppid=,command="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        pid, ppid, command = int(parts[0]), int(parts[1]), parts[2]
        if pid == me or ppid == me or "mavsdk_server" not in command.split(None, 1)[0]:
            continue
        for address in addresses:
            port = _port_of(address)
            if address in command or (port and re.search(rf":{port}(?!\d)", command)):
                found.append((pid, command))
                break
    return found


def wait_port_released(address: str, timeout_s: float = 10.0) -> None:
    """Wait until a vehicle's port can be used again.

    For ``udpin`` this binds the port until that succeeds. For ``tcpout`` it only pauses 2 s: ArduCopter serves
    one client per serial port, so a connect-and-close probe would itself take the slot.
    """
    if address.strip().startswith("udpin://"):
        port = _port_of(address)
        if port is None:
            return
        deadline = time.monotonic() + timeout_s
        while True:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("0.0.0.0", int(port)))
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
            finally:
                sock.close()
            time.sleep(0.2)
    else:
        time.sleep(TCP_RELEASE_PAUSE_S)


def with_autopilot(env: SitlEnv, autopilot: str | None) -> SitlEnv:
    """A copy of *env* with *autopilot* filled in."""
    return replace(env, autopilot=autopilot)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, link_factory: Callable[[str], Any] | None = None, out: Any = None) -> int:
    """``wait-ready``: probe every address at once and print one line each; exit 0 only when all are ready."""
    parser = argparse.ArgumentParser(prog="python -m tests.integration.sitl_support")
    sub = parser.add_subparsers(dest="command", required=True)
    ready = sub.add_parser("wait-ready", help="wait until every vehicle may be flown")
    ready.add_argument("--timeout", type=float, required=True, help="seconds to wait for each vehicle")
    ready.add_argument("--address", action="append", required=True, help="a vehicle's MAVSDK address (repeatable)")
    args = parser.parse_args(argv)
    out = sys.stdout if out is None else out

    def probe(address: str) -> tuple[bool, str]:
        try:
            report = wait_ready(address, args.timeout, link_factory)
        except SitlNotReady as exc:
            text = str(exc)
            prefix = f"{address}: "
            return False, f"NOT READY {address}: {text[len(prefix):] if text.startswith(prefix) else text}"
        except Exception as exc:  # noqa: BLE001 - one bad address must not hide the others
            return False, f"NOT READY {address}: {type(exc).__name__}: {exc}"
        sysid = "unknown" if report.sysid is None else report.sysid
        return True, (
            f"READY {report.address} autopilot={report.autopilot or 'unknown'} sysid={sysid} "
            f"after {report.waited_s:.1f} s"
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(args.address)) as pool:
        results = list(pool.map(probe, args.address))
    for _, line in results:
        print(line, file=out, flush=True)
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
