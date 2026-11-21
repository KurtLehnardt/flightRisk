"""Claude API uplink check (flightrisk/launcher/claude_uplink.py, scripts/check_claude_uplink.py).

DNS, sockets and adapter listing are all injected or mocked: nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import socket
import ssl
from dataclasses import replace
from pathlib import Path

import pytest

from flightrisk.launcher import claude_uplink as cu
from flightrisk.launcher.commands import EdgeToken
from flightrisk.launcher.netinfo import Adapter
from flightrisk.launcher.preflight import FAIL, PASS, WARN, PreflightEnv, run_preflight
from flightrisk.launcher.settings import LauncherSettings, builtin_presets

SECRET = "sk-ant-this-must-never-appear"
GOOD_ENV = {cu.PROVIDER_ENV: "claude", cu.ALLOW_ENV: "1", cu.KEY_ENV: SECRET}
ADAPTERS = [
    Adapter("Wi-Fi", "192.168.1.20", 21, True),
    Adapter("Wi-Fi 2", "192.168.10.50", 10, True),
    Adapter("Wi-Fi 5", "192.168.10.2", 26, True),
]


def make_env(environ=None, *, resolve=None, local="192.168.1.20", tls=None, found=True):
    def default_tls(host, ip, timeout):
        return "TLSv1.3"

    return cu.UplinkEnv(
        resolve=resolve or (lambda host: ["160.79.104.10"]),
        route_local_ip=lambda ip: local,
        tls_handshake=tls or default_tls,
        adapters=lambda: ADAPTERS,
        module_found=lambda name: found,
        environ=GOOD_ENV if environ is None else environ,
    )


def by_name(rows):
    return {row.name: row for row in rows}


def test_all_good_passes_and_names_the_uplink_adapter():
    rows = by_name(cu.check_claude_uplink(make_env()))
    assert not cu.has_failure(list(rows.values()))
    route = rows["Route to 160.79.104.10"]
    assert route.status == cu.PASS
    assert "192.168.1.20 on Wi-Fi (index 21)" in route.detail
    assert rows["TLS api.anthropic.com:443"].status == cu.PASS


def test_route_via_tello_subnet_warns():
    rows = by_name(cu.check_claude_uplink(make_env(local="192.168.10.50")))
    route = rows["Route to 160.79.104.10"]
    assert route.status == cu.WARN
    assert "Wi-Fi 2" in route.detail and "192.168.10.0/24" in route.detail
    assert "metric" in route.detail


@pytest.mark.parametrize("ip,expected", [("192.168.10.1", True), ("192.168.10.255", True), ("192.168.11.1", False), ("10.42.0.1", False), ("junk", False)])
def test_on_tello_subnet(ip, expected):
    assert cu.on_tello_subnet(ip) is expected


def test_dns_failure_fails_and_stops_before_tls():
    def boom(host):
        raise socket.gaierror(-2, "Name or service not known")

    def no_tls(*a):
        raise AssertionError("must not connect when DNS fails")

    rows = cu.check_claude_uplink(make_env(resolve=boom, tls=no_tls))
    assert by_name(rows)["DNS api.anthropic.com"].status == cu.FAIL
    assert cu.has_failure(rows)
    assert not any(r.name.startswith("TLS") for r in rows)


def test_tls_timeout_fails():
    def slow(host, ip, timeout):
        raise TimeoutError("timed out")

    rows = by_name(cu.check_claude_uplink(make_env(tls=slow)))
    assert rows["TLS api.anthropic.com:443"].status == cu.FAIL
    assert "TimeoutError" in rows["TLS api.anthropic.com:443"].detail


def test_tls_certificate_error_fails():
    def bad(host, ip, timeout):
        raise ssl.SSLCertVerificationError("bad cert")

    assert cu.has_failure(cu.check_claude_uplink(make_env(tls=bad)))


def test_no_default_route_fails():
    rows = by_name(cu.check_claude_uplink(make_env(local=None)))
    assert rows["Route to 160.79.104.10"].status == cu.FAIL


def test_timeout_is_passed_to_the_handshake():
    seen = {}

    def tls(host, ip, timeout):
        seen.update(host=host, ip=ip, timeout=timeout)
        return "TLSv1.3"

    cu.check_claude_uplink(make_env(tls=tls), timeout=1.5)
    assert seen == {"host": "api.anthropic.com", "ip": "160.79.104.10", "timeout": 1.5}


@pytest.mark.parametrize("missing", [cu.ALLOW_ENV, cu.KEY_ENV])
def test_missing_env_var_fails(missing):
    environ = {k: v for k, v in GOOD_ENV.items() if k != missing}
    rows = by_name(cu.check_claude_uplink(make_env(environ)))
    assert rows[missing].status == cu.FAIL


def test_empty_key_counts_as_unset_and_allow_needs_an_on_value():
    rows = by_name(cu.check_claude_uplink(make_env({**GOOD_ENV, cu.KEY_ENV: "", cu.ALLOW_ENV: "0"})))
    assert rows[cu.KEY_ENV].status == cu.FAIL
    assert rows[cu.ALLOW_ENV].status == cu.FAIL


def test_missing_anthropic_package_fails():
    rows = by_name(cu.check_claude_uplink(make_env(found=False)))
    assert rows["anthropic package"].status == cu.FAIL


def test_key_value_never_appears_in_any_row():
    for env in (make_env(), make_env(local="192.168.10.50"), make_env(local=None)):
        text = repr(cu.check_claude_uplink(env))
        assert SECRET not in text


def test_adapter_listing_failure_does_not_break_the_route_row():
    env = make_env()
    env.adapters = lambda: (_ for _ in ()).throw(RuntimeError("no netsh"))
    route = by_name(cu.check_claude_uplink(env))["Route to 160.79.104.10"]
    assert route.status == cu.PASS and "192.168.1.20" in route.detail


def test_provider_is_claude():
    assert cu.provider_is_claude({cu.PROVIDER_ENV: " Claude "})
    assert not cu.provider_is_claude({cu.PROVIDER_ENV: "gemma"})
    assert not cu.provider_is_claude({})


# --- the default socket-backed functions, with sockets and DNS mocked -----------------------------


def test_default_resolve_uses_getaddrinfo_ipv4_and_dedupes(monkeypatch):
    calls = []

    def fake(host, port, family, type_):
        calls.append((host, port, family))
        return [(family, type_, 6, "", ("160.79.104.10", port)), (family, type_, 6, "", ("160.79.104.10", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    assert cu._resolve("api.anthropic.com") == ["160.79.104.10"]
    assert calls == [("api.anthropic.com", 443, socket.AF_INET)]


def test_default_route_uses_a_udp_connect_and_sends_nothing(monkeypatch):
    log = []

    class FakeUdp:
        def __init__(self, family, kind):
            log.append(("open", kind))

        def connect(self, addr):
            log.append(("connect", addr))

        def getsockname(self):
            return ("192.168.1.20", 55555)

        def send(self, *a):
            raise AssertionError("no packet may be sent")

        sendto = send

        def close(self):
            log.append(("close",))

    monkeypatch.setattr(socket, "socket", FakeUdp)
    assert cu._route_local_ip("160.79.104.10") == "192.168.1.20"
    assert log == [("open", socket.SOCK_DGRAM), ("connect", ("160.79.104.10", 443)), ("close",)]


def test_default_route_returns_none_when_connect_fails(monkeypatch):
    class Unroutable:
        def __init__(self, *a):
            pass

        def connect(self, addr):
            raise OSError("Network is unreachable")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", Unroutable)
    assert cu._route_local_ip("160.79.104.10") is None


def test_default_tls_handshake_connects_with_timeout_and_sends_no_data(monkeypatch):
    seen = {}

    class FakeTls:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def version(self):
            return "TLSv1.3"

        def send(self, *a):
            raise AssertionError("nothing may be sent")

        sendall = send

    class FakeRaw(FakeTls):
        pass

    class FakeContext:
        def wrap_socket(self, raw, server_hostname):
            seen["sni"] = server_hostname
            return FakeTls()

    def fake_connect(addr, timeout):
        seen["addr"], seen["timeout"] = addr, timeout
        return FakeRaw()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeContext())
    assert cu._tls_handshake("api.anthropic.com", "160.79.104.10", 2.0) == "TLSv1.3"
    assert seen == {"addr": ("160.79.104.10", 443), "timeout": 2.0, "sni": "api.anthropic.com"}


# --- the launcher preflight wiring -----------------------------------------------------------------


def preflight(settings, environ, rows=None, calls=None):
    def fake_uplink(env, adapters=None, network=True):
        if calls is not None:
            calls.append(dict(env))
        return rows if rows is not None else [cu.UplinkRow("TLS api.anthropic.com:443", cu.PASS, "ok")]

    env = PreflightEnv(
        in_venv=lambda: True, module_found=lambda m: True, port_free=lambda h, p: True,
        tcp_reachable=lambda h, p: True, adapters=lambda: ADAPTERS,
        weights=lambda: [{"name": "default", "label": "d", "weights": "yolo11n.pt", "required": False, "found": True, "fallback": None}],
        ceiling_file=lambda: (settings.ceiling_cm, settings.margin_cm, None, "ceiling.json"),
        environ=environ, claude_uplink=fake_uplink,
    )
    return run_preflight(settings, EdgeToken("set", "env"), env, probe_drones=False)


def layout_b(**kw):
    return replace(builtin_presets()["Layout B (everything here)"], **kw)


def test_preflight_skips_the_uplink_unless_provider_is_claude():
    calls = []
    checks = preflight(layout_b(llm=True), {cu.PROVIDER_ENV: "gemma"}, calls=calls)
    assert calls == [] and not any(c.name.startswith("Claude") for c in checks)
    preflight(layout_b(llm=True), {}, calls=calls)
    assert calls == []


def test_preflight_runs_the_uplink_for_a_claude_ground():
    settings = layout_b(llm=True)
    calls = []
    checks = preflight(settings, GOOD_ENV, calls=calls)
    assert len(calls) == 1
    assert any(c.name == "Claude: TLS api.anthropic.com:443" and c.status == PASS for c in checks)
    assert not any(c.name == "Language model" for c in checks)


def test_preflight_uplink_failure_warns_and_does_not_block():
    settings = layout_b(llm=True)
    rows = [cu.UplinkRow("DNS api.anthropic.com", cu.FAIL, "does not resolve")]
    checks = preflight(settings, GOOD_ENV, rows=rows)
    row = next(c for c in checks if c.name == "Claude: DNS api.anthropic.com")
    assert row.status == WARN
    assert not any(c.status == FAIL and c.name.startswith("Claude") for c in checks)


def test_preflight_warns_when_llm_toggle_is_off():
    settings = layout_b(llm=False)
    checks = preflight(settings, GOOD_ENV)
    row = next(c for c in checks if c.name == "Language model")
    assert row.status == WARN and "--no-llm" in row.detail


def test_preflight_no_uplink_rows_for_an_edge_only_role():
    edge = replace(layout_b(llm=True), role="edges")
    calls = []
    preflight(edge, GOOD_ENV, calls=calls)
    assert calls == []


# --- the script ----------------------------------------------------------------------------------


def load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "check_claude_uplink.py"
    spec = importlib.util.spec_from_file_location("check_claude_uplink", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_script_skips_with_exit_zero_when_provider_is_not_claude(capsys):
    script = load_script()
    assert script.main([], make_env({cu.PROVIDER_ENV: "gemma"})) == 0
    assert "skipped" in capsys.readouterr().out


def test_script_exits_zero_on_success_and_prints_no_secret(capsys):
    script = load_script()
    assert script.main([], make_env()) == 0
    out = capsys.readouterr().out
    assert "OK:" in out and SECRET not in out


def test_script_exits_nonzero_on_failure(capsys):
    script = load_script()

    def down(host, ip, timeout):
        raise OSError("unreachable")

    assert script.main([], make_env(tls=down)) == 1
    assert "FAIL" in capsys.readouterr().out


def test_script_force_runs_for_other_providers(capsys):
    script = load_script()
    assert script.main(["--force"], make_env({cu.PROVIDER_ENV: "gemma"})) == 1  # env vars missing


# --- review round 1: timeouts, the route row without DNS, secrets ------------------------------------


def test_a_hanging_dns_lookup_is_cut_off_and_still_shows_the_route():
    import threading
    import time

    release = threading.Event()

    def hang(host):
        release.wait(30)
        return ["160.79.104.10"]

    def no_tls(*a):
        raise AssertionError("must not connect when DNS timed out")

    started = time.monotonic()
    try:
        rows = by_name(cu.check_claude_uplink(make_env(resolve=hang, tls=no_tls), timeout=0.2))
    finally:
        release.set()
    assert time.monotonic() - started < 3
    assert rows["DNS api.anthropic.com"].status == cu.FAIL
    assert "TimeoutError" in rows["DNS api.anthropic.com"].detail


def test_dns_failure_still_probes_the_route_to_a_literal_anthropic_address():
    def boom(host):
        raise socket.gaierror(-2, "Name or service not known")

    rows = by_name(cu.check_claude_uplink(make_env(resolve=boom, local="192.168.10.50")))
    route = rows[f"Route to {cu.FALLBACK_IP}"]
    assert route.status == cu.WARN and "Wi-Fi 2" in route.detail and "DNS failed" in route.detail


def test_the_handshake_gets_only_what_is_left_of_the_budget():
    seen = {}

    def tls(host, ip, timeout):
        seen["timeout"] = timeout
        return "TLSv1.3"

    cu.check_claude_uplink(make_env(tls=tls), timeout=4.0, budget=0.0)
    assert seen["timeout"] == 0.5


def test_a_stalled_handshake_is_cut_off_at_the_deadline_and_reports_it():
    import threading
    import time

    release = threading.Event()

    def stalled(host, ip, timeout):
        release.wait(30)
        return "TLSv1.3"

    started = time.monotonic()
    try:
        rows = by_name(cu.check_claude_uplink(make_env(tls=stalled), timeout=0.6, budget=5.0))
    finally:
        release.set()
    assert time.monotonic() - started < 3
    tls = rows["TLS api.anthropic.com:443"]
    assert tls.status == cu.FAIL and "TimeoutError" in tls.detail
    assert "within 0.6 s" in tls.detail


def test_a_spent_budget_still_gives_dns_the_floor_not_a_zero_deadline():
    # budget=0 used to mean a DNS deadline of 0, so join(0) raced the resolver.
    rows = by_name(cu.check_claude_uplink(make_env(), timeout=4.0, budget=0.0))
    assert rows["DNS api.anthropic.com"].status == cu.PASS


def test_network_false_returns_only_the_variable_rows():
    def never(*a):
        raise AssertionError("no network when the language model is off")

    env = make_env()
    env.resolve = never
    rows = cu.check_claude_uplink(env, network=False)
    assert {r.name for r in rows} == {cu.ALLOW_ENV, cu.KEY_ENV, "anthropic package"}


def test_the_uplink_env_repr_does_not_contain_the_key():
    assert SECRET not in repr(make_env())


def test_preflight_makes_no_network_check_with_the_model_off_and_passes_the_adapters():
    seen = {}

    def fake(environ, adapters, network):
        seen.update(adapters=adapters, network=network)
        return []

    settings = layout_b(llm=False)
    env = PreflightEnv(
        in_venv=lambda: True, module_found=lambda m: True, port_free=lambda h, p: True,
        tcp_reachable=lambda h, p: True, adapters=lambda: ADAPTERS,
        weights=lambda: [], ceiling_file=lambda: (settings.ceiling_cm, settings.margin_cm, None, "c.json"),
        environ=GOOD_ENV, claude_uplink=fake,
    )
    run_preflight(settings, EdgeToken("set", "env"), env, probe_drones=False)
    assert seen == {"adapters": ADAPTERS, "network": False}


def test_a_crashing_uplink_check_is_a_warning_and_never_fails_start():
    def crash(environ, adapters, network):
        raise RuntimeError("boom")

    settings = layout_b(llm=True)
    env = PreflightEnv(
        in_venv=lambda: True, module_found=lambda m: True, port_free=lambda h, p: True,
        tcp_reachable=lambda h, p: True, adapters=lambda: ADAPTERS,
        weights=lambda: [], ceiling_file=lambda: (settings.ceiling_cm, settings.margin_cm, None, "c.json"),
        environ=GOOD_ENV, claude_uplink=crash,
    )
    checks = run_preflight(settings, EdgeToken("set", "env"), env, probe_drones=False)
    row = next(c for c in checks if c.name == "Claude: uplink check")
    assert row.status == WARN and "RuntimeError" in row.detail
