"""Network adapter parsing for the launcher (flightrisk/launcher/netinfo.py)."""

from __future__ import annotations

import json

from flightrisk.launcher.netinfo import (
    Adapter,
    address_owners,
    list_adapters,
    parse_get_netipaddress,
    parse_ifconfig,
    parse_ip_addr,
)

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 11:22:33:44:55:66
\tinet 192.168.10.2 netmask 0xffffff00 broadcast 192.168.10.255
\tstatus: active
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 10.42.0.1 netmask 0xffffff00 broadcast 10.42.0.255
\tstatus: inactive
en9: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
\tstatus: inactive
"""


def test_parse_ifconfig():
    indexes = {"lo0": 1, "en0": 14, "en5": 8}
    adapters = parse_ifconfig(IFCONFIG, index_of=indexes.get)
    assert adapters == [
        Adapter("lo0", "127.0.0.1", 1, True),
        Adapter("en0", "192.168.10.2", 14, True),
        Adapter("en5", "10.42.0.1", 8, False),
    ]
    assert adapters[0].loopback and not adapters[1].loopback


def test_parse_ip_addr():
    text = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "3: wlan0    inet 192.168.10.3/24 brd 192.168.10.255 scope global dynamic wlan0\n"
    )
    assert parse_ip_addr(text) == [Adapter("lo", "127.0.0.1", 1), Adapter("wlan0", "192.168.10.3", 3)]


def test_parse_get_netipaddress_list_and_single_object():
    rows = [
        {"InterfaceIndex": 12, "InterfaceAlias": "Wi-Fi", "IPAddress": "192.168.10.2", "AddressState": 4},
        {"InterfaceIndex": 23, "InterfaceAlias": "Wi-Fi 2", "IPAddress": "192.168.10.50", "AddressState": 1},
        {"InterfaceIndex": 1, "InterfaceAlias": "Loopback", "IPAddress": "127.0.0.1"},
    ]
    adapters = parse_get_netipaddress(json.dumps(rows))
    assert adapters[0] == Adapter("Wi-Fi", "192.168.10.2", 12, True)
    assert adapters[1].up is False
    assert adapters[2].up is None
    single = parse_get_netipaddress(json.dumps(rows[0]))
    assert single == [adapters[0]]
    assert parse_get_netipaddress("") == [] and parse_get_netipaddress("garbage") == []


def test_list_adapters_picks_the_tool_per_platform_and_sorts():
    calls = []

    def run(argv):
        calls.append(argv[0])
        return json.dumps([
            {"InterfaceIndex": 23, "InterfaceAlias": "Wi-Fi 2", "IPAddress": "192.168.10.50"},
            {"InterfaceIndex": 12, "InterfaceAlias": "Wi-Fi", "IPAddress": "192.168.10.2"},
        ])

    adapters = list_adapters(run=run, platform="win32")
    assert calls == ["powershell"]
    assert [a.index for a in adapters] == [12, 23]


def test_list_adapters_survives_a_missing_tool():
    def run(argv):
        raise FileNotFoundError(argv[0])

    assert list_adapters(run=run, platform="darwin") == []


def test_address_owners():
    adapters = [Adapter("a", "192.168.10.2", 1), Adapter("b", "192.168.10.2", 2), Adapter("c", "10.0.0.1", 3)]
    assert [a.name for a in address_owners(adapters, "192.168.10.2")] == ["a", "b"]
    assert address_owners(adapters, "1.1.1.1") == []
