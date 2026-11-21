"""The Windows guard against a stray wildcard holder on a Tello port (``check_no_wildcard_holder``).

On Windows a program bound to 0.0.0.0:8889 does not stop an edge bound to
one adapter's IP from starting, yet the Tello's replies to that program are
delivered to the edge. The edge therefore reads the UDP endpoint table at
start and refuses when any socket holds 0.0.0.0 or [::] on its Tello ports,
while another edge's sockets on its own adapter IP are allowed. Every test
here uses a stubbed endpoint table; nothing is bound to a Tello port.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import types
from collections import namedtuple

import pytest

from flightrisk.drone import tello_bound
from flightrisk.drone import udp_endpoints as udp_mod
from flightrisk.drone.tello import TelloController
from flightrisk.drone.tello_bound import (
    TelloBinding,
    TelloBindingError,
    check_binding,
    check_no_wildcard_holder,
)
from flightrisk.drone.udp_endpoints import (
    UdpEndpoint,
    netstat_command,
    parse_netstat,
    read_udp_endpoints,
    wildcard_holders,
)

TELLO_PORTS = (8889, 8890, 11111)
NETSTAT = 'netstat -ano -p udp | findstr ":8889 :8890 :11111"'


class _ProbeSocket:
    """Stands in for the throwaway probe socket of ``check_binding``; records binds."""

    def __init__(self, log: list):
        self.log = log

    def bind(self, address):
        self.log.append(address)

    def close(self):
        pass


def _edge(ip: str, pid: int) -> list[UdpEndpoint]:
    """The three sockets one edge holds: every Tello port on its own adapter IP."""
    return [UdpEndpoint(ip, port, pid) for port in TELLO_PORTS]


# Wildcard sockets every Windows computer has, none on a Tello port.
SYSTEM = [UdpEndpoint("0.0.0.0", 5353, 2200), UdpEndpoint("0.0.0.0", 123, 1400), UdpEndpoint("::", 500, 3100),
          UdpEndpoint("127.0.0.1", 8889, 7000)]


def _check(local_ip: str, endpoints, **overrides) -> list:
    """Run ``check_binding`` as Windows would, with the endpoint table ``endpoints``. Returns the probe binds."""
    log: list = []
    kwargs = {"socket_factory": lambda: _ProbeSocket(log), "interface_table": dict,
              "is_windows": lambda: True, "udp_endpoints": lambda: list(endpoints)}
    kwargs.update(overrides)
    check_binding(TelloBinding(local_ip), "192.168.10.1", **kwargs)
    return log


@pytest.fixture(autouse=True)
def _process_names(monkeypatch):
    """Name every PID python.exe, so no real process is looked up."""
    monkeypatch.setattr(tello_bound, "process_name", lambda pid: "python.exe")


# ---------------------------------------------------------------------------
# The four cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stray", [
    UdpEndpoint("0.0.0.0", 8889, 4321),
    UdpEndpoint("0.0.0.0", 8890, 4321),
    UdpEndpoint("0.0.0.0", 11111, 4321),
    UdpEndpoint("::", 8889, 4321),
])
def test_a_stray_wildcard_holder_is_refused(stray):
    """A djitellopy script or an edge without --tello-local-ip holds a Tello port on all addresses."""
    with pytest.raises(TelloBindingError) as exc:
        _check("192.168.10.50", SYSTEM + _edge("192.168.10.2", 100) + [stray])
    message = str(exc.value)
    assert f"{stray.address()} (PID 4321, python.exe)" in message
    assert "on all addresses" in message
    assert NETSTAT in message
    # The sibling edge on its own adapter IP is not named.
    assert "192.168.10.2" not in message
    if stray.ipv6:
        assert 'netstat -ano -p udpv6 | findstr ":8889 :8890 :11111"' in message
    else:
        assert "udpv6" not in message


def test_a_sibling_edge_on_another_local_ip_is_allowed():
    """Both edges bind 8889, 8890 and 11111, each on its own adapter IP: neither refuses the other."""
    edge_1 = _edge("192.168.10.2", 100)
    edge_2 = _edge("192.168.10.50", 200)
    _check("192.168.10.50", SYSTEM + edge_1)
    # Edge-1 restarting while edge-2 runs.
    _check("192.168.10.2", SYSTEM + edge_2)


def test_the_first_edge_alone_is_allowed():
    _check("192.168.10.2", SYSTEM)
    _check("192.168.10.2", [])


def test_non_windows_is_a_no_op():
    """Off Windows the endpoint table is never read, whatever holds the ports."""
    def unread():
        pytest.fail("the endpoint table was read off Windows")

    log = _check("192.168.10.2", [], is_windows=lambda: False, udp_endpoints=unread)
    assert log == [("192.168.10.2", 0)]
    check_no_wildcard_holder(TelloBinding("192.168.10.2"), is_windows=lambda: False, udp_endpoints=unread)


@pytest.mark.skipif(sys.platform == "win32", reason="checks the real platform default off Windows")
def test_the_default_platform_check_reads_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(udp_mod, "read_udp_endpoints", lambda: pytest.fail("the endpoint table was read"))
    check_no_wildcard_holder(TelloBinding("192.168.10.2"))


# ---------------------------------------------------------------------------
# Details of the check
# ---------------------------------------------------------------------------


def test_only_the_bindings_own_ports_count():
    binding = TelloBinding("192.168.10.2", cmd_port=9001, state_port=9002, video_port=9003)
    check_no_wildcard_holder(binding, is_windows=lambda: True,
                             udp_endpoints=lambda: [UdpEndpoint("0.0.0.0", 8889, 1)])
    with pytest.raises(TelloBindingError, match=r'0\.0\.0\.0:9002.*findstr ":9001 :9002 :9003"'):
        check_no_wildcard_holder(binding, is_windows=lambda: True,
                                 udp_endpoints=lambda: [UdpEndpoint("0.0.0.0", 9002, 1)])


def test_a_holder_without_a_pid_is_still_refused():
    with pytest.raises(TelloBindingError, match=r"UDP 0\.0\.0\.0:8889 on all addresses"):
        check_no_wildcard_holder(TelloBinding("192.168.10.2"), is_windows=lambda: True,
                                 udp_endpoints=lambda: [UdpEndpoint("0.0.0.0", 8889)])


def test_an_unreadable_endpoint_table_is_logged_and_allowed(capsys):
    _check("192.168.10.2", [], udp_endpoints=lambda: None)
    out = capsys.readouterr().out
    assert "not checked" in out
    assert NETSTAT in out


def test_the_edge_refuses_to_start_with_a_stray_holder(monkeypatch):
    """Both startup paths run the check: the early check in edge_device and the controller's own."""
    from flightrisk import edge_device

    monkeypatch.setattr(tello_bound, "_is_windows", lambda: True)
    monkeypatch.setattr(udp_mod, "read_udp_endpoints", lambda: [UdpEndpoint("0.0.0.0", 8890, 4321)])
    args = argparse.Namespace(tello_local_ip="127.0.0.1", tello_ifindex=None, tello_host="127.0.0.1")
    with pytest.raises(SystemExit, match=r"0\.0\.0\.0:8890 .*Refusing to start"):
        edge_device._check_tello_binding(args)

    binds: list = []

    class RecordingSocket(socket.socket):
        def bind(self, address):
            binds.append(address)
            return super().bind(address)

    def factory() -> socket.socket:
        return RecordingSocket(socket.AF_INET, socket.SOCK_DGRAM)

    with pytest.raises(TelloBindingError, match="on all addresses"):
        TelloController(name="drone-1", host="127.0.0.1", binding=TelloBinding("127.0.0.1"), socket_factory=factory)
    # Only the throwaway probe was bound: no Tello port was opened.
    assert binds == [("127.0.0.1", 0)]


# ---------------------------------------------------------------------------
# Reading the endpoint table
# ---------------------------------------------------------------------------


NETSTAT_OUTPUT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1104
  TCP    192.168.10.2:8889      192.168.10.1:8889      ESTABLISHED     55
  TCP    [::]:445               [::]:0                 LISTENING       4
  UDP    0.0.0.0:5353           *:*                                    2200
  UDP    0.0.0.0:8889           *:*                                    4321
  UDP    192.168.10.2:8889      *:*                                    100
  UDP    192.168.10.50:11111    *:*                                    200
  udp    127.0.0.1:50000        *:*                                    300
  UDP    [::]:11111             *:*                                    4322
  UDP    [fe80::1c2d:3e4f:5a6b:7c8d%12]:1900  *:*                      900
  UDP    garbage                *:*                                    1
"""


def test_netstat_output_is_parsed():
    assert parse_netstat(NETSTAT_OUTPUT) == [
        UdpEndpoint("0.0.0.0", 5353, 2200),
        UdpEndpoint("0.0.0.0", 8889, 4321),
        UdpEndpoint("192.168.10.2", 8889, 100),
        UdpEndpoint("192.168.10.50", 11111, 200),
        UdpEndpoint("127.0.0.1", 50000, 300),
        UdpEndpoint("::", 11111, 4322),
        UdpEndpoint("fe80::1c2d:3e4f:5a6b:7c8d", 1900, 900),
    ]
    holders = wildcard_holders(parse_netstat(NETSTAT_OUTPUT), TELLO_PORTS)
    assert [h.address() for h in holders] == ["0.0.0.0:8889", "[::]:11111"]


def test_netstat_is_run_without_name_lookups():
    calls: list = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=NETSTAT_OUTPUT, stderr="")

    assert udp_mod._netstat_endpoints(run=run)[1] == UdpEndpoint("0.0.0.0", 8889, 4321)
    assert calls[0][0] == ["netstat", "-ano"]
    assert calls[0][1]["timeout"] > 0


def test_psutil_endpoints_are_read(monkeypatch):
    addr = namedtuple("addr", "ip port")
    conn = namedtuple("conn", "laddr pid")
    fake = types.SimpleNamespace(net_connections=lambda kind: [
        conn(addr("0.0.0.0", 8889), 4321), conn(addr("::", 11111), None), conn((), 5),
        conn(addr("fe80::1%12", 1900), 900),
    ])
    monkeypatch.setitem(sys.modules, "psutil", fake)
    assert udp_mod._psutil_endpoints() == [
        UdpEndpoint("0.0.0.0", 8889, 4321), UdpEndpoint("::", 11111, None), UdpEndpoint("fe80::1", 1900, 900),
    ]


def test_the_first_working_reader_wins_and_none_means_unreadable():
    entry = UdpEndpoint("0.0.0.0", 8889, 1)

    def broken():
        raise OSError("netstat missing")

    assert read_udp_endpoints([broken, lambda: [entry]]) == [entry]
    assert read_udp_endpoints([broken, broken]) is None


def test_the_netstat_command_names_the_tello_ports():
    assert netstat_command(TELLO_PORTS) == NETSTAT
    assert netstat_command(TELLO_PORTS, ipv6=True) == 'netstat -ano -p udpv6 | findstr ":8889 :8890 :11111"'
