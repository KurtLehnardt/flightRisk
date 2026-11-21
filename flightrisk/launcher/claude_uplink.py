"""Read-only check that this computer can reach the Claude API, for ground stations on FLIGHTRISK_REASONING_PROVIDER=claude.

It resolves ``api.anthropic.com``, opens a TLS connection to port 443 with a short timeout and closes it
after the handshake without sending an HTTP request, and reports which local address the operating system routes that traffic
from. It makes no authenticated API call and never reads a key's value: the three Claude variables are
reported only as set or unset. A route that leaves through a Tello's Wi-Fi (192.168.10.0/24) is warned
about, because a Tello access point has no internet.

Cross-platform (no Windows-only calls). All side effects go through ``UplinkEnv`` so tests can inject
fakes. Used by the launcher's preflight and by ``scripts/check_claude_uplink.py``.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from flightrisk.launcher.netinfo import Adapter, address_owners, list_adapters

API_HOST = "api.anthropic.com"
API_PORT = 443
TIMEOUT_S = 4.0
#: The whole check (DNS, route and TLS) is cut off after about this long, so Start never waits on it for longer.
BUDGET_S = 5.0
#: No deadline is shorter than this, so a budget that is already spent still gives each step a real chance.
DEADLINE_FLOOR_S = 0.5
#: A literal address of api.anthropic.com (Anthropic's 160.79.104.0/23), used to still show which adapter the
#: route leaves from when DNS does not answer.
FALLBACK_IP = "160.79.104.10"
TELLO_SUBNET = ipaddress.ip_network("192.168.10.0/24")

PROVIDER_ENV = "FLIGHTRISK_REASONING_PROVIDER"
ALLOW_ENV = "FLIGHTRISK_ALLOW_CLOUD_LLM"
KEY_ENV = "FLIGHTRISK_CLAUDE_API_KEY"

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"


@dataclass(frozen=True)
class UplinkRow:
    """One result row; the launcher turns each into a preflight ``Check``."""

    name: str
    status: str
    detail: str = ""


def _resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, API_PORT, socket.AF_INET, socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def _call_with_deadline(fn: Callable[[], Any], deadline_s: float) -> Any:
    """Run *fn* on a daemon thread and give up after *deadline_s* seconds.

    ``getaddrinfo`` has no timeout of its own and can block for a minute or more; the abandoned thread
    is a daemon, so it never holds the process open. Raises ``TimeoutError`` (an ``OSError``) on expiry.
    """
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="claude-uplink-dns", daemon=True)
    thread.start()
    thread.join(max(deadline_s, DEADLINE_FLOOR_S))
    if thread.is_alive():
        raise TimeoutError(f"no answer within {deadline_s:g} s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["value"]  # type: ignore[return-value]


def _route_local_ip(remote_ip: str) -> str | None:
    """The local address the OS would send to *remote_ip* from. A UDP connect sends no packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_ip, API_PORT))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _tls_handshake(host: str, remote_ip: str, timeout: float) -> str:
    """Complete a TLS handshake with *host* at *remote_ip*, then close. Returns the TLS version."""
    context = ssl.create_default_context()
    with socket.create_connection((remote_ip, API_PORT), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            return tls.version() or "TLS"


def _module_found(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class UplinkEnv:
    resolve: Callable[[str], list[str]] = _resolve
    route_local_ip: Callable[[str], str | None] = _route_local_ip
    tls_handshake: Callable[[str, str, float], str] = _tls_handshake
    adapters: Callable[[], list[Adapter]] = list_adapters
    module_found: Callable[[str], bool] = _module_found
    # repr=False: the environ holds the Claude key, and a dataclass repr would print it.
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)


def provider_is_claude(environ: Mapping[str, str]) -> bool:
    return (environ.get(PROVIDER_ENV) or "").strip().lower() == "claude"


def on_tello_subnet(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in TELLO_SUBNET
    except ValueError:
        return False


def _env_rows(environ: Mapping[str, str], module_found: Callable[[str], bool]) -> list[UplinkRow]:
    """Set or unset only; no value is ever put in a row."""
    rows = []
    allow = (environ.get(ALLOW_ENV) or "").strip().lower() in ("1", "true", "yes", "on")
    rows.append(UplinkRow(
        f"{ALLOW_ENV}", PASS if allow else FAIL,
        "set to an on value" if allow else "unset or not 1; the ground refuses the cloud model without it (setx, then restart the shell)",
    ))
    has_key = bool(environ.get(KEY_ENV))
    rows.append(UplinkRow(
        f"{KEY_ENV}", PASS if has_key else FAIL,
        "set" if has_key else "unset; ANTHROPIC_API_KEY is not read (setx, then restart the shell)",
    ))
    have_pkg = module_found("anthropic")
    rows.append(UplinkRow(
        "anthropic package", PASS if have_pkg else FAIL,
        "installed" if have_pkg else "missing; reinstall with FLIGHTRISK_ROLE=ground (docs/fleet-setup.md) or pip install anthropic",
    ))
    return rows


def check_claude_uplink(
    env: UplinkEnv | None = None, *, timeout: float = TIMEOUT_S, host: str = API_HOST,
    budget: float = BUDGET_S, network: bool = True,
) -> list[UplinkRow]:
    """Every uplink row. Any ``FAIL`` means Claude reasoning will not work from this computer.

    The whole check is held to about *budget* seconds: DNS runs on a thread with a deadline, and the TLS
    handshake gets what is left. With ``network=False`` only the variable and package rows are returned.
    """
    env = env or UplinkEnv()
    rows = _env_rows(env.environ, env.module_found)
    if not network:
        return rows
    started = time.monotonic()

    dns_failed = False
    try:
        addresses = _call_with_deadline(lambda: env.resolve(host), min(timeout, budget))
    except OSError as exc:
        rows.append(UplinkRow(f"DNS {host}", FAIL, f"does not resolve ({type(exc).__name__}: {exc}); no internet uplink, or DNS goes to a Tello"))
        dns_failed = True
    else:
        if not addresses:
            rows.append(UplinkRow(f"DNS {host}", FAIL, "resolved to no IPv4 address"))
            dns_failed = True
    if dns_failed:
        # The route does not need DNS: probe a literal Anthropic address so a bad adapter still shows.
        rows.append(_route_row(env, FALLBACK_IP, "(a literal api.anthropic.com address, DNS failed)"))
        return rows
    ip = addresses[0]
    rows.append(UplinkRow(f"DNS {host}", PASS, ", ".join(addresses[:3])))
    rows.append(_route_row(env, ip, ""))

    remaining = budget - (time.monotonic() - started)
    tls_deadline = min(timeout, max(remaining, DEADLINE_FLOOR_S))
    try:
        # The socket timeout applies per operation, so the whole handshake also runs under one deadline.
        version = _call_with_deadline(lambda: env.tls_handshake(host, ip, tls_deadline), tls_deadline)
    except (OSError, ssl.SSLError) as exc:
        rows.append(UplinkRow(f"TLS {host}:{API_PORT}", FAIL, f"handshake failed within {tls_deadline:g} s ({type(exc).__name__}: {exc})"))
    else:
        rows.append(UplinkRow(f"TLS {host}:{API_PORT}", PASS, f"{version} handshake ok; no HTTP request was sent"))
    return rows


def _route_row(env: UplinkEnv, ip: str, note: str) -> UplinkRow:
    name = f"Route to {ip}"
    suffix = f" {note}" if note else ""
    local = env.route_local_ip(ip)
    if local is None:
        return UplinkRow(name, FAIL, "this computer has no route to it (no default route out)" + suffix)
    owners = address_owners(_safe_adapters(env.adapters), local)
    via = f"{local} on {owners[0].name}" + (f" (index {owners[0].index})" if owners[0].index is not None else "") if owners else local
    if on_tello_subnet(local):
        return UplinkRow(
            name, WARN,
            f"leaves from {via}, a Tello subnet (192.168.10.0/24) with no internet. Set the uplink's interface metric "
            f"lower than both Tello Wi-Fi adapters (docs/fleet-setup.md, Layout B with Claude reasoning){suffix}",
        )
    return UplinkRow(name, PASS, f"leaves from {via}{suffix}")


def _safe_adapters(lister: Callable[[], list[Adapter]]) -> list[Adapter]:
    try:
        return lister()
    except Exception:  # noqa: BLE001 - adapter names are only decoration for the route row
        return []


def has_failure(rows: list[UplinkRow]) -> bool:
    return any(row.status == FAIL for row in rows)
