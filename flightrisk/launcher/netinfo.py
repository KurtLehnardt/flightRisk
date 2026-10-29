"""This computer's network adapters and their IPv4 addresses, using the operating system's own tools.

macOS and Linux: ``ifconfig`` or ``ip -4 -o addr``, with the interface index
from ``socket.if_nametoindex``. Windows: ``Get-NetIPAddress`` through
PowerShell, which gives the ``InterfaceIndex`` that ``--tello-ifindex`` needs.
The parsers are plain functions so they can be tested with captured output.
Nothing here sends a packet.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class Adapter:
    """One IPv4 address on one adapter."""

    name: str
    ipv4: str
    index: int | None = None
    up: bool | None = None

    @property
    def loopback(self) -> bool:
        try:
            return ipaddress.IPv4Address(self.ipv4).is_loopback
        except ValueError:
            return False

    def to_json(self) -> dict:
        return {"name": self.name, "ipv4": self.ipv4, "index": self.index, "up": self.up, "loopback": self.loopback}


Runner = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False, **kwargs)
    return result.stdout or ""


def _index_of(name: str) -> int | None:
    try:
        return socket.if_nametoindex(name)
    except (OSError, AttributeError):
        return None


def parse_ifconfig(text: str, index_of: Callable[[str], int | None] = _index_of) -> list[Adapter]:
    """Adapters from macOS or BSD ``ifconfig`` output."""
    adapters: list[Adapter] = []
    current: str | None = None
    addresses: list[str] = []
    up: bool | None = None
    status: bool | None = None

    def flush() -> None:
        if current is not None:
            state = status if status is not None else up
            adapters.extend(Adapter(current, ip, index_of(current), state) for ip in addresses)

    for line in text.splitlines():
        header = re.match(r"^([A-Za-z0-9_.\-]+):\s+flags=\d+<([^>]*)>", line)
        if header:
            flush()
            current, addresses, status = header.group(1), [], None
            up = "UP" in header.group(2).split(",")
            continue
        stripped = line.strip()
        inet = re.match(r"^inet\s+(\d+\.\d+\.\d+\.\d+)", stripped)
        if inet and current is not None:
            addresses.append(inet.group(1))
        elif stripped.startswith("status:"):
            status = stripped.split(":", 1)[1].strip() == "active"
    flush()
    return adapters


def parse_ip_addr(text: str) -> list[Adapter]:
    """Adapters from Linux ``ip -4 -o addr show`` output."""
    adapters = []
    for line in text.splitlines():
        match = re.match(r"^(\d+):\s+([^\s:]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", line.strip())
        if match:
            adapters.append(Adapter(match.group(2), match.group(3), int(match.group(1)), None))
    return adapters


def parse_get_netipaddress(text: str) -> list[Adapter]:
    """Adapters from ``Get-NetIPAddress -AddressFamily IPv4 | Select ... | ConvertTo-Json``."""
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    rows = data if isinstance(data, list) else [data]
    adapters = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = str(row.get("IPAddress") or "").strip()
        alias = str(row.get("InterfaceAlias") or "").strip()
        index = row.get("InterfaceIndex")
        state = row.get("AddressState")
        if not ip:
            continue
        adapters.append(Adapter(
            alias or f"interface {index}", ip,
            index if isinstance(index, int) and not isinstance(index, bool) else None,
            # AddressState 4 (or "Preferred") means the address is usable.
            None if state is None else state in (4, "Preferred"),
        ))
    return adapters


_PS_COMMAND = (
    "Get-NetIPAddress -AddressFamily IPv4 | "
    "Select-Object InterfaceIndex,InterfaceAlias,IPAddress,AddressState | ConvertTo-Json -Compress"
)


def list_adapters(run: Runner = _run, platform: str | None = None) -> list[Adapter]:
    """Every IPv4 address on this computer, sorted by interface index. An empty list when the tools fail."""
    platform = sys.platform if platform is None else platform
    try:
        if platform == "win32":
            adapters = parse_get_netipaddress(run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_COMMAND]))
        elif platform.startswith("linux"):
            adapters = parse_ip_addr(run(["ip", "-4", "-o", "addr", "show"]))
        else:
            adapters = parse_ifconfig(run(["ifconfig"]))
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(adapters, key=lambda a: (a.index is None, a.index or 0, a.name, a.ipv4))


def address_owners(adapters: list[Adapter], ip: str) -> list[Adapter]:
    """The adapters that hold *ip*."""
    return [a for a in adapters if a.ipv4 == ip]


def adapters_for_host(adapters: list[Adapter], host: str) -> list[Adapter]:
    """The non-loopback adapters with an IPv4 address in *host*'s /24, so any of them could reach it.

    A Tello's own Wi-Fi is a /24 (192.168.10.0/24), and every Tello answers on
    192.168.10.1, so when this returns more than one adapter, a socket bound to
    all addresses may reach a different drone than the one meant.
    """
    try:
        network = ipaddress.IPv4Network(f"{host.strip()}/24", strict=False)
    except ValueError:
        return []
    found = []
    for adapter in adapters:
        if adapter.loopback:
            continue
        try:
            if ipaddress.IPv4Address(adapter.ipv4) in network:
                found.append(adapter)
        except ValueError:
            continue
    return found
