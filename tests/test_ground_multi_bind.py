"""Tests for the ground's multi-host bind.

The ground of a two-laptop fleet listens on loopback (for the edge on the same
laptop) and on the laptop-to-laptop link address (for the other laptop's
edge), never on a wildcard address. These tests cover the parsing and safety
rules for the list of bind hosts, the per-host listeners, and what happens
when the link address is missing at startup or disappears while running.

There is no Thunderbolt link in a test run, so the link address
(``10.42.0.1``) is simulated: ``websockets.serve`` is patched so a bind of
the link address either fails with ``EADDRNOTAVAIL`` (address missing) or
opens a real listener on 127.0.0.1 on its own ephemeral port, which the
"remote" edge then connects to. Loopback listeners are always real.
"""

import asyncio
import errno
import json
import os
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import websockets

from flightrisk.transport import (
    EdgeTransport,
    GroundTransport,
    GroundTransportSync,
    _address_is_local,
    check_bind_hosts,
    is_wildcard_host,
    parse_bind_hosts,
)

LINK = "10.42.0.1"
TOKEN = "multi-bind-test-token"


class EventLog:
    """Collects StructuredLogger-style events."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def _record(self, level, event, **fields):
        self.events.append((level, event, fields))

    def info(self, event, **fields):
        self._record("info", event, **fields)

    def warning(self, event, **fields):
        self._record("warning", event, **fields)

    def error(self, event, **fields):
        self._record("error", event, **fields)

    def debug(self, event, **fields):
        self._record("debug", event, **fields)

    def named(self, event):
        return [fields for _, name, fields in self.events if name == event]


@pytest.fixture
def link(monkeypatch):
    """Simulate the link address: present or missing, under test control."""
    real_serve = websockets.serve
    state = SimpleNamespace(
        present=True,       # whether the address exists (probe result and bind outcome)
        bind_errno=errno.EADDRNOTAVAIL,
        requested_ports=[],  # the port each bind of LINK asked for
        port=None,           # the real port the simulated link listener got
        serve_calls=[],      # every host passed to websockets.serve
    )

    def fake_serve(handler, host, port, **kwargs):
        state.serve_calls.append(host)
        if host != LINK:
            return real_serve(handler, host, port, **kwargs)
        state.requested_ports.append(port)
        if not state.present:
            async def fail():
                raise OSError(state.bind_errno, os.strerror(state.bind_errno))
            return fail()

        async def open_alias():
            server = await real_serve(handler, "127.0.0.1", 0, **kwargs)
            state.port = server.sockets[0].getsockname()[1]
            return server
        return open_alias()

    monkeypatch.setattr(websockets, "serve", fake_serve)
    state.probe = lambda host: state.present if host == LINK else True
    return state


def _ground(link_state, host=f"127.0.0.1,{LINK}", token=TOKEN, **kwargs) -> GroundTransport:
    kwargs.setdefault("bind_check_interval_s", 3600.0)  # tests drive recheck_binds() themselves
    kwargs.setdefault("event_log", EventLog())
    return GroundTransport(host=host, port=0, token=token, address_probe=link_state.probe, **kwargs)


async def _edge(port: int, token: str | None = TOKEN) -> EdgeTransport:
    edge = EdgeTransport(ws_url=f"ws://127.0.0.1:{port}", token=token, max_retries=0)
    await edge.connect()
    return edge


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def _recv_type(edge: EdgeTransport, mtype: str, timeout: float = 2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        msg = await edge.recv(timeout=remaining)
        if msg is None or msg.get("type") == mtype:
            return msg


# ---------------------------------------------------------------------------
# Parsing and safety rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("127.0.0.1", ("127.0.0.1",)),
        ("0.0.0.0", ("0.0.0.0",)),
        ("", ("",)),  # the legacy single host keeps its meaning: every interface
        (" 10.42.0.1 ", ("10.42.0.1",)),
        ("127.0.0.1,10.42.0.1", ("127.0.0.1", "10.42.0.1")),
        (" 127.0.0.1 , 10.42.0.1 ", ("127.0.0.1", "10.42.0.1")),
        ("127.0.0.1,10.42.0.1,127.0.0.1", ("127.0.0.1", "10.42.0.1")),
        (["127.0.0.1", "::1"], ("127.0.0.1", "::1")),
        (("10.42.0.1",), ("10.42.0.1",)),
    ],
)
def test_parse_bind_hosts(setting, expected):
    assert parse_bind_hosts(setting) == expected


@pytest.mark.parametrize("setting", ["127.0.0.1,", ",", "127.0.0.1,,10.42.0.1", ["127.0.0.1", " "], []])
def test_parse_bind_hosts_refuses_an_empty_entry(setting):
    # An empty entry must never turn into the wildcard "".
    with pytest.raises(ValueError):
        parse_bind_hosts(setting)


def test_parse_bind_hosts_refuses_non_strings():
    with pytest.raises(TypeError):
        parse_bind_hosts(["127.0.0.1", 9000])


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "", " ", "::0", "0:0:0:0:0:0:0:0", "::ffff:0.0.0.0", "0", "0.0", "0x0", "000.000.000.000"],
)
def test_wildcard_spellings(host):
    assert is_wildcard_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", LINK, "fe80::1", "ground.local"])
def test_not_wildcard(host):
    assert not is_wildcard_host(host)


@pytest.mark.parametrize(
    "hosts",
    [
        ("127.0.0.1", "0.0.0.0"),
        ("0.0.0.0", LINK),
        ("::", "127.0.0.1"),
        ("127.0.0.1", "::0"),
        ("0.0.0.0",),  # a one-entry list is still the list form
    ],
)
def test_list_with_wildcard_is_refused_even_with_token(hosts):
    with pytest.raises(ValueError, match="wildcard"):
        check_bind_hosts(hosts, TOKEN, list_form=True)


def test_list_entry_must_be_a_literal_address():
    with pytest.raises(ValueError, match="literal IP address"):
        check_bind_hosts(("127.0.0.1", "ground.local"), TOKEN, list_form=True)
    # localhost is loopback and stays allowed.
    check_bind_hosts(("localhost", LINK), TOKEN, list_form=True)


def test_list_with_non_loopback_needs_token():
    with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
        check_bind_hosts(("127.0.0.1", LINK), None, list_form=True)
    with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
        check_bind_hosts(("127.0.0.1", LINK), "", list_form=True)
    check_bind_hosts(("127.0.0.1", LINK), TOKEN, list_form=True)


def test_loopback_only_list_needs_no_token():
    check_bind_hosts(("127.0.0.1", "::1", "localhost"), None, list_form=True)


def test_legacy_single_host_rules_unchanged():
    # A single wildcard host keeps working with a token, as before.
    check_bind_hosts(("0.0.0.0",), TOKEN)
    with pytest.raises(ValueError, match="non-loopback host '0.0.0.0'"):
        check_bind_hosts(("0.0.0.0",), None)
    # A single name keeps working with a token (it is not literal-checked).
    check_bind_hosts(("ground.local",), TOKEN)
    check_bind_hosts(("127.0.0.1",), None)


def test_empty_token_does_not_authorise_an_exposed_bind():
    gt = GroundTransport(host="0.0.0.0", port=0, token="", event_log=EventLog())

    async def scenario():
        with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
            await gt.start()
        assert gt.bound_hosts == ()

    asyncio.run(scenario())


def test_address_probe_reports_loopback_present_and_test_net_absent():
    assert _address_is_local("127.0.0.1")
    assert _address_is_local("ground.local")  # a name cannot be probed; the bind decides
    if _address_is_local("192.0.2.1"):
        pytest.skip("this host can bind TEST-NET-1 addresses (non-local bind enabled)")
    assert not _address_is_local("192.0.2.1")


# ---------------------------------------------------------------------------
# Listeners
# ---------------------------------------------------------------------------


def test_loopback_only_list_binds_without_token(link):
    async def scenario():
        gt = _ground(link, host=["127.0.0.1"], token=None)
        await gt.start()
        edge = None
        try:
            assert gt.bound_hosts == ("127.0.0.1",)
            assert gt.unbound_hosts == ()
            edge = await _edge(gt.port, token=None)
            assert await _wait_until(lambda: len(gt.clients) == 1)
            assert gt._bind_watch_task is None  # nothing to watch
        finally:
            if edge is not None:
                await edge.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_loopback_and_link_both_serve_edges(link):
    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        local = remote = None
        try:
            assert gt.hosts == ("127.0.0.1", LINK)
            assert gt.bound_hosts == ("127.0.0.1", LINK)
            # Loopback binds first and fixes the port; the link reuses it.
            assert link.serve_calls == ["127.0.0.1", LINK]
            assert link.requested_ports == [gt.port]

            local = await _edge(gt.port)
            remote = await _edge(link.port)
            assert await _wait_until(lambda: len(gt._authenticated_clients) == 2)

            assert await gt.send_heartbeat() == 2
            assert await _recv_type(local, "heartbeat") is not None
            assert await _recv_type(remote, "heartbeat") is not None
            assert await gt.emergency_land_all() == 2

            listening = log.named("ground_bind_listening")
            assert [(e["host"], e["loopback"]) for e in listening] == [("127.0.0.1", True), (LINK, False)]
            assert TOKEN not in json.dumps(log.events)
        finally:
            for edge in (local, remote):
                if edge is not None:
                    await edge.disconnect()
            await gt.stop()
        assert gt.bound_hosts == ()

    asyncio.run(scenario())


def test_loopback_and_link_without_token_refused_before_binding(link):
    async def scenario():
        gt = _ground(link, token=None)
        with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
            await gt.start()
        assert link.serve_calls == []
        assert gt.bound_hosts == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("host", ["127.0.0.1,0.0.0.0", f"0.0.0.0,{LINK}", ["::", "127.0.0.1"], f"127.0.0.1,{LINK},::"])
def test_wildcard_in_list_refused_before_binding(link, host):
    async def scenario():
        gt = _ground(link, host=host)
        with pytest.raises(ValueError, match="wildcard"):
            await gt.start()
        assert link.serve_calls == []

    asyncio.run(scenario())


def test_name_in_list_refused_before_binding(link):
    async def scenario():
        gt = _ground(link, host="127.0.0.1,ground.local")
        with pytest.raises(ValueError, match="literal IP address"):
            await gt.start()
        assert link.serve_calls == []

    asyncio.run(scenario())


def test_link_missing_at_start_runs_on_loopback_and_recovers(link):
    """The bridge has no address yet: the local edge flies, the link binds when the address returns."""
    link.present = False

    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        local = remote = None
        try:
            assert gt.bound_hosts == ("127.0.0.1",)
            assert gt.unbound_hosts == (LINK,)
            unavailable = log.named("ground_bind_unavailable")
            assert len(unavailable) == 1
            assert unavailable[0]["host"] == LINK
            assert unavailable[0]["errno"] == errno.EADDRNOTAVAIL
            assert unavailable[0]["listening"] == ["127.0.0.1"]

            local = await _edge(gt.port)
            assert await _wait_until(lambda: len(gt._authenticated_clients) == 1)
            assert await gt.send_heartbeat() == 1

            # Still missing: no bind attempt, no new log.
            await gt.recheck_binds()
            assert link.requested_ports == [gt.port]
            assert len(log.named("ground_bind_unavailable")) == 1

            # The address is back: the link listener binds on the same port.
            link.present = True
            await gt.recheck_binds()
            assert gt.bound_hosts == ("127.0.0.1", LINK)
            assert gt.unbound_hosts == ()
            assert link.requested_ports == [gt.port, gt.port]
            assert [e["host"] for e in log.named("ground_bind_recovered")] == [LINK]

            remote = await _edge(link.port)
            assert await _wait_until(lambda: len(gt._authenticated_clients) == 2)
            assert local.connected  # the loopback edge never dropped
            assert await gt.send_heartbeat() == 2
        finally:
            for edge in (local, remote):
                if edge is not None:
                    await edge.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_link_bind_retry_logs_each_distinct_error_once(link):
    link.present = False

    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        try:
            # The probe now says present, but the bind keeps failing (port taken).
            link.probe = lambda host: True
            gt._address_probe = link.probe
            link.bind_errno = errno.EADDRINUSE
            for _ in range(3):
                await gt.recheck_binds()
            codes = [e["errno"] for e in log.named("ground_bind_unavailable")]
            assert codes == [errno.EADDRNOTAVAIL, errno.EADDRINUSE]
            assert gt.bound_hosts == ("127.0.0.1",)
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_link_address_lost_while_running_keeps_every_listener_and_connection(link):
    """Cable pulled: the address vanishes. Nothing is closed; the loss and the return are logged."""

    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        local = remote = None
        try:
            local = await _edge(gt.port)
            remote = await _edge(link.port)
            assert await _wait_until(lambda: len(gt._authenticated_clients) == 2)
            servers = dict(gt._servers)

            link.present = False
            await gt.recheck_binds()
            await gt.recheck_binds()  # logged once, not every check
            lost = log.named("ground_bind_address_lost")
            assert len(lost) == 1
            assert lost[0]["host"] == LINK
            assert lost[0]["listener"] == "kept"
            assert gt._servers == servers
            assert gt.bound_hosts == ("127.0.0.1", LINK)

            # The local edge is untouched and still hears the ground.
            assert local.connected
            assert await gt.send_heartbeat() == 2
            assert await _recv_type(local, "heartbeat") is not None

            link.present = True
            await gt.recheck_binds()
            assert [e["host"] for e in log.named("ground_bind_address_restored")] == [LINK]
            assert gt._servers == servers  # the same listeners, no rebind
            assert log.named("ground_bind_recovered") == []
            assert remote.connected
        finally:
            for edge in (local, remote):
                if edge is not None:
                    await edge.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_bind_watcher_rebinds_on_its_own(link):
    link.present = False

    async def scenario():
        gt = _ground(link, bind_check_interval_s=0.02)
        await gt.start()
        try:
            assert gt._bind_watch_task is not None
            await asyncio.sleep(0.1)
            assert gt.bound_hosts == ("127.0.0.1",)
            link.present = True
            assert await _wait_until(lambda: gt.bound_hosts == ("127.0.0.1", LINK))
        finally:
            await gt.stop()
        assert gt._bind_watch_task is None
        assert gt.bound_hosts == ()

    asyncio.run(scenario())


def test_bind_watcher_survives_a_failing_probe(link):
    calls = []

    def bad_probe(host):
        calls.append(host)
        raise RuntimeError("interface query failed")

    async def scenario():
        gt = _ground(link, bind_check_interval_s=0.02)
        gt._address_probe = bad_probe
        await gt.start()
        local = None
        try:
            local = await _edge(gt.port)
            assert await _wait_until(lambda: len(calls) >= 3)
            assert not gt._bind_watch_task.done()
            assert local.connected
            assert await gt.send_heartbeat() == 1
        finally:
            if local is not None:
                await local.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_loopback_bind_failure_raises_and_link_never_binds(link):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]

    async def scenario():
        gt = GroundTransport(
            host=f"{LINK},127.0.0.1", port=taken, token=TOKEN,
            address_probe=link.probe, event_log=EventLog(),
        )
        with pytest.raises(OSError):
            await gt.start()
        assert LINK not in link.serve_calls  # loopback is bound first
        assert gt.bound_hosts == ()
        assert gt._server is None

    try:
        asyncio.run(scenario())
    finally:
        blocker.close()


def test_single_non_loopback_host_failure_still_raises(link):
    """Without a loopback host to fall back on, a failed bind fails start(), as before."""
    link.present = False

    async def scenario():
        for host in (LINK, [LINK]):
            gt = _ground(link, host=host)
            with pytest.raises(OSError):
                await gt.start()
            assert gt._server is None

    asyncio.run(scenario())


def test_stop_then_start_again(link):
    async def scenario():
        gt = _ground(link)
        await gt.start()
        await gt.stop()
        assert gt.bound_hosts == ()
        await gt.start()
        try:
            assert gt.bound_hosts == ("127.0.0.1", LINK)
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_sync_wrapper_accepts_a_host_list(link):
    gt = GroundTransportSync(host=f"127.0.0.1,{LINK}", port=0, token=TOKEN)
    gt._transport._address_probe = link.probe
    gt._transport._event_log = EventLog()
    gt.start()
    try:
        assert gt._transport.bound_hosts == ("127.0.0.1", LINK)
        assert gt.port == gt._transport.port
    finally:
        gt.stop()


def test_legacy_single_string_host_unchanged():
    async def scenario():
        gt = GroundTransport(host="127.0.0.1", port=0, event_log=EventLog())
        await gt.start()
        edge = None
        try:
            assert gt.hosts == ("127.0.0.1",)
            assert gt.bound_hosts == ("127.0.0.1",)
            edge = await _edge(gt.port, token=None)
            assert await _wait_until(lambda: len(gt.clients) == 1)
        finally:
            if edge is not None:
                await edge.disconnect()
            await gt.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Dashboard command line
# ---------------------------------------------------------------------------


def _run_dashboard_main(argv, env=None):
    import flightrisk.dashboard.__main__ as dashboard_main

    environ = {key: value for key, value in os.environ.items()
               if key not in ("FLIGHTRISK_FLEET_NET", "FLIGHTRISK_EDGE_HOST", "FLIGHTRISK_FLEET_DRONES")}
    environ.update(env or {})
    with patch.object(dashboard_main, "run_dashboard") as run, \
            patch.dict(os.environ, environ, clear=True), \
            patch.object(sys, "argv", ["flightrisk-dashboard"] + argv):
        dashboard_main.main()
    return run.call_args[0][0]


def test_cli_passes_a_host_list_through():
    config = _run_dashboard_main(["--source", "edge", "--edge-host", f"127.0.0.1,{LINK}"])
    assert config.edge_host == f"127.0.0.1,{LINK}"
    config = _run_dashboard_main(["--source", "edge"], env={"FLIGHTRISK_EDGE_HOST": f"127.0.0.1,{LINK}"})
    assert config.edge_host == f"127.0.0.1,{LINK}"


def test_cli_refuses_a_malformed_host_list():
    with pytest.raises(SystemExit):
        _run_dashboard_main(["--source", "edge", "--edge-host", "127.0.0.1,"])


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0", "::", "127.0.0.1,0.0.0.0",
        # Alternate spellings the OS resolver reads as 0.0.0.0, and a name that may resolve to it.
        "0", "0.0", "0x0", "000.000.000.000", "127.0.0.1,0x0", "wildcard.example",
    ],
)
def test_cli_refuses_wildcard_in_ap_fleet_mode(host):
    with pytest.raises(SystemExit):
        _run_dashboard_main(
            ["--source", "edge", "--edge-host", host, "--fleet", "drone-1,drone-2"],
            env={"FLIGHTRISK_FLEET_NET": "ap"},
        )


def test_cli_keeps_legacy_wildcard_outside_ap_fleet_mode():
    # Station fleet mode and a non-fleet ground keep accepting a single wildcard host.
    config = _run_dashboard_main(
        ["--source", "edge", "--edge-host", "0.0.0.0", "--fleet", "drone-1,drone-2"],
        env={"FLIGHTRISK_FLEET_NET": "station"},
    )
    assert config.edge_host == "0.0.0.0"
    config = _run_dashboard_main(["--source", "edge", "--edge-host", "0.0.0.0"], env={"FLIGHTRISK_FLEET_NET": "ap"})
    assert config.edge_host == "0.0.0.0"


def test_cli_accepts_loopback_and_link_literals_in_ap_fleet_mode():
    for host in ("127.0.0.1", LINK, f"127.0.0.1,{LINK}", "localhost"):
        config = _run_dashboard_main(
            ["--source", "edge", "--edge-host", host, "--fleet", "drone-1,drone-2"],
            env={"FLIGHTRISK_FLEET_NET": "ap"},
        )
        assert config.edge_host == host


@pytest.mark.parametrize("host", ["0", "0x0", "0.0", "000.000.000.000", "wildcard.example"])
def test_alternate_wildcard_spellings_in_a_list_are_refused_before_binding(link, host):
    async def scenario():
        gt = _ground(link, host=["127.0.0.1", host])
        with pytest.raises(ValueError):
            await gt.start()
        assert gt.bound_hosts == ()
        assert link.serve_calls == []

    asyncio.run(scenario())
    with pytest.raises(ValueError):
        check_bind_hosts((host,), TOKEN, list_form=True)


def test_single_host_wildcard_spelling_keeps_the_token_rule():
    # The single legacy form is unchanged: an explicit wildcard still works with a token.
    check_bind_hosts(("0",), TOKEN)
    with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
        check_bind_hosts(("0",), None)


@pytest.mark.parametrize("token", ["", " ", "   ", "\t\n"])
def test_blank_token_does_not_authorise_an_exposed_bind(token):
    with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
        check_bind_hosts(("0.0.0.0",), token)
    with pytest.raises(ValueError, match="FLIGHTRISK_EDGE_TOKEN"):
        check_bind_hosts(("127.0.0.1", LINK), token, list_form=True)


@pytest.mark.parametrize("err", [errno.EADDRINUSE, errno.EACCES, errno.EINVAL])
def test_link_bind_error_other_than_missing_address_fails_start(link, err):
    """Something else owns the link address: the ground must not start loopback-only."""
    link.present = False
    link.bind_errno = err

    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        with pytest.raises(OSError) as info:
            await gt.start()
        assert info.value.errno == err
        assert LINK in str(info.value)
        assert gt.bound_hosts == ()
        assert gt._server is None
        assert gt._bind_watch_task is None
        assert log.named("ground_bind_unavailable") == []

    asyncio.run(scenario())


def test_windows_missing_address_code_still_degrades(link):
    link.present = False
    link.bind_errno = 10049  # WSAEADDRNOTAVAIL

    async def scenario():
        gt = _ground(link)
        await gt.start()
        try:
            assert gt.bound_hosts == ("127.0.0.1",)
            assert gt.unbound_hosts == (LINK,)
        finally:
            await gt.stop()

    asyncio.run(scenario())


def test_loopback_aliases_in_a_list_are_one_clear_error():
    with pytest.raises(ValueError, match="loopback twice"):
        parse_bind_hosts("127.0.0.1,localhost")
    with pytest.raises(ValueError, match="loopback twice"):
        parse_bind_hosts(["localhost", "::1", LINK])
    assert parse_bind_hosts("::1,0:0:0:0:0:0:0:1") == ("::1",)


def test_sync_wrapper_exposes_bound_and_unbound_hosts():
    gts = GroundTransportSync(host="127.0.0.1", port=0)
    assert gts.bound_hosts == ()
    assert gts.unbound_hosts == ()
    gts.start()
    try:
        assert gts.bound_hosts == ("127.0.0.1",)
        assert gts.unbound_hosts == ()
    finally:
        gts._bridge.run(gts._transport.stop())


def test_restored_address_with_unreachable_listener_is_rebound(link):
    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        remote = None
        try:
            old = gt._servers[LINK]
            link.present = False
            await gt.recheck_binds()
            real_self_connect = gt._self_connect
            calls = []

            async def broken_once(address, port):
                calls.append((address, port))
                return False if len(calls) == 1 else await real_self_connect(address, port)

            gt._self_connect = broken_once
            link.present = True
            await gt.recheck_binds()
            assert len(calls) == 1
            assert gt._servers[LINK] is not old
            assert len(log.named("ground_bind_listener_unreachable")) == 1
            assert [e["host"] for e in log.named("ground_bind_listener_rebound")] == [LINK]
            assert gt.bound_hosts == ("127.0.0.1", LINK)
            remote = await _edge(link.port)
            assert await _wait_until(lambda: len(gt._authenticated_clients) == 1)
        finally:
            if remote is not None:
                await remote.disconnect()
            await gt.stop()

    asyncio.run(scenario())


def test_restored_address_with_working_listener_is_kept(link):
    async def scenario():
        log = EventLog()
        gt = _ground(link, event_log=log)
        await gt.start()
        try:
            old = gt._servers[LINK]
            link.present = False
            await gt.recheck_binds()
            link.present = True
            await gt.recheck_binds()  # real self-connect to the simulated listener succeeds
            assert gt._servers[LINK] is old
            assert log.named("ground_bind_listener_rebound") == []
            assert len(log.named("ground_bind_address_restored")) == 1
        finally:
            await gt.stop()

    asyncio.run(scenario())
