"""This computer's UDP endpoint table: which local address and port each socket holds, and its process.

Used on Windows only, by ``flightrisk.drone.tello_bound``, to find a program
that holds the wildcard address on a Tello port (see
``docs/two-tellos-one-pc.md``). The table is the one ``netstat -ano`` prints
(Windows ``GetExtendedUdpTable``). It is read with ``psutil`` when it is
installed (the edge has it through Ultralytics), and otherwise by parsing
``netstat -ano``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass

__all__ = [
    "WILDCARD_IPS",
    "UdpEndpoint",
    "netstat_command",
    "parse_netstat",
    "process_name",
    "read_udp_endpoints",
    "wildcard_holders",
]

#: The wildcard (all addresses) IPv4 and IPv6 addresses, as the table shows them.
WILDCARD_IPS = frozenset({"0.0.0.0", "::"})

# netstat can be slow on a busy computer, but never this slow.
_NETSTAT_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class UdpEndpoint:
    """One UDP socket's local address. ``ip`` has no brackets ("::", not "[::]")."""

    ip: str
    port: int
    pid: int | None = None

    @property
    def wildcard(self) -> bool:
        return self.ip in WILDCARD_IPS

    @property
    def ipv6(self) -> bool:
        return ":" in self.ip

    def address(self) -> str:
        """The address as netstat prints it: ``0.0.0.0:8889`` or ``[::]:8889``."""
        return f"[{self.ip}]:{self.port}" if self.ipv6 else f"{self.ip}:{self.port}"


def netstat_command(ports: Iterable[int], *, ipv6: bool = False) -> str:
    """The netstat command an operator runs to see who holds these UDP ports."""
    pattern = " ".join(f":{port}" for port in ports)
    return f'netstat -ano -p {"udpv6" if ipv6 else "udp"} | findstr "{pattern}"'


def _split_address(text: str) -> tuple[str, int] | None:
    """``0.0.0.0:8889`` or ``[::]:8889`` (or ``[fe80::1%12]:8889``) as (ip, port), or None."""
    if text.startswith("["):
        close = text.find("]")
        if close < 0 or text[close + 1:close + 2] != ":":
            return None
        ip, port = text[1:close], text[close + 2:]
    else:
        ip, sep, port = text.rpartition(":")
        if not sep:
            return None
    ip = ip.split("%", 1)[0]
    if not ip or not port.isdigit():
        return None
    return ip, int(port)


def parse_netstat(text: str) -> list[UdpEndpoint]:
    """The UDP rows of ``netstat -ano`` output (IPv4 and IPv6). Other rows and the headers are skipped.

    A UDP row is ``UDP  <local address>  *:*  <PID>``. The protocol name and
    the addresses are not translated on a localized Windows; the headers
    are, and they are ignored.
    """
    endpoints = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0].upper() != "UDP":
            continue
        address = _split_address(fields[1])
        if address is None:
            continue
        pid = int(fields[-1]) if fields[-1].isdigit() else None
        endpoints.append(UdpEndpoint(address[0], address[1], pid))
    return endpoints


def _psutil_endpoints() -> list[UdpEndpoint]:
    import psutil

    endpoints = []
    for conn in psutil.net_connections(kind="udp"):
        if not conn.laddr:
            continue
        endpoints.append(UdpEndpoint(conn.laddr.ip.split("%", 1)[0], conn.laddr.port, conn.pid))
    return endpoints


def _netstat_endpoints(run: Callable = subprocess.run) -> list[UdpEndpoint]:
    result = run(
        ["netstat", "-ano"], capture_output=True, text=True, errors="replace",
        timeout=_NETSTAT_TIMEOUT_S, check=True,
    )
    return parse_netstat(result.stdout)


def read_udp_endpoints(
    readers: Iterable[Callable[[], list[UdpEndpoint]]] | None = None,
) -> list[UdpEndpoint] | None:
    """Every UDP socket's local address on this computer, or None when the table cannot be read.

    Tries ``psutil`` and then ``netstat -ano``; the first that works wins.
    """
    for reader in readers if readers is not None else (_psutil_endpoints, _netstat_endpoints):
        try:
            return reader()
        except Exception:  # noqa: BLE001, S112 - psutil or netstat missing or failing: try the next reader
            continue
    return None


def wildcard_holders(endpoints: Iterable[UdpEndpoint], ports: Iterable[int]) -> list[UdpEndpoint]:
    """The endpoints bound to 0.0.0.0 or [::] on one of ``ports``, sorted, each once.

    A socket bound to a specific address (another edge's adapter IP) is not
    a holder: it only receives datagrams sent to that address.
    """
    wanted = set(ports)
    holders = {e for e in endpoints if e.wildcard and e.port in wanted}
    return sorted(holders, key=lambda e: (e.port, e.ipv6, e.pid if e.pid is not None else -1))


def process_name(pid: int | None) -> str | None:
    """The program name of ``pid`` (for example ``python.exe``), or None when it cannot be found."""
    if pid is None:
        return None
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - psutil missing, process gone or not ours to inspect
        return None
