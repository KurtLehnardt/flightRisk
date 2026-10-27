"""Tests for scripts/check_fleet_link.py argument handling and soak summary (local sockets only)."""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_fleet_link", ROOT / "scripts" / "check_fleet_link.py")
cfl = importlib.util.module_from_spec(_spec)
sys.modules["check_fleet_link"] = cfl
_spec.loader.exec_module(cfl)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_soak_summary_clean_run():
    samples = [(i * 0.1, 1.0 + i) for i in range(10)]  # rtt 1..10 ms
    s = cfl.soak_summary(samples)
    assert s["sent"] == 10 and s["lost"] == 0 and s["loss_pct"] == 0.0
    assert s["rtt_p50_ms"] == 5.0
    assert s["rtt_p95_ms"] == 10.0
    assert s["rtt_max_ms"] == 10.0
    assert s["max_gap_s"] < 0.2
    ok, text = cfl.format_soak(s)
    assert ok and "PASS" in text


def test_soak_summary_loss_and_gap():
    samples = [(0.0, 1.0), (0.1, 1.0), (0.2, None), (0.3, None), (0.4, None), (0.5, None), (0.6, 2.0), (0.7, 1.0)]
    s = cfl.soak_summary(samples)
    assert s["lost"] == 4 and s["loss_pct"] == 50.0
    assert s["max_gap_s"] == pytest.approx(0.5, abs=0.01)  # reply at 0.101 to reply at 0.602
    ok, text = cfl.format_soak(s)
    assert not ok and "FAIL" in text and "loss 50.0%" in text


def test_soak_summary_dead_link_and_empty():
    s = cfl.soak_summary([(0.0, None), (1.0, None), (2.0, None)])
    assert s["received"] == 0 and s["loss_pct"] == 100.0 and s["rtt_max_ms"] is None
    assert s["max_gap_s"] == 2.0
    ok, text = cfl.format_soak(s)
    assert not ok and "no replies" in text
    assert cfl.soak_summary([])["sent"] == 0


def test_soak_over_local_socket():
    port = _free_port()

    async def scenario():
        server = asyncio.create_task(cfl.run_server("127.0.0.1", port, 3.0))
        await asyncio.sleep(0.3)
        samples = await cfl.ws_soak(f"ws://127.0.0.1:{port}", 0.5, source="127.0.0.1")
        server.cancel()
        return samples

    samples = asyncio.run(scenario())
    s = cfl.soak_summary(samples)
    assert s["sent"] >= 4
    assert s["lost"] == 0
    assert s["rtt_max_ms"] < 500


def test_soak_counts_unreachable_peer_as_lost():
    samples = asyncio.run(cfl.ws_soak(f"ws://127.0.0.1:{_free_port()}", 0.3, reply_timeout_s=0.2))
    s = cfl.soak_summary(samples)
    assert s["sent"] >= 2 and s["received"] == 0 and s["loss_pct"] == 100.0


def test_client_args_pass_bind_peer_soak(monkeypatch):
    seen = {}
    monkeypatch.setattr(cfl, "run_client", lambda *a: seen.setdefault("args", a) and True)
    assert cfl.main(["--role", "client", "--peer", "10.9.9.9", "--bind", "10.9.9.8", "--soak", "5", "--port", "9100"]) == 0
    assert seen["args"] == ("10.9.9.9", 9100, "10.9.9.8", 5.0)


def test_client_defaults_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(cfl, "run_client", lambda *a: seen.setdefault("args", a) and True)
    cfl.main(["--role", "client"])
    assert seen["args"] == ("10.42.0.1", 9000, None, None)


def test_server_defaults_unchanged(monkeypatch):
    calls = {}
    monkeypatch.setattr(cfl, "ping_host", lambda host, timeout_s=3.0, source=None: calls.setdefault("ping", (host, source)) and True)

    async def fake_server(bind, port, duration):
        calls["serve"] = (bind, port, duration)
        return True

    monkeypatch.setattr(cfl, "run_server", fake_server)
    assert cfl.main(["--role", "server"]) == 0
    assert calls["ping"] == ("10.42.0.2", None)
    assert calls["serve"] == ("10.42.0.1", 9000, None)
    calls.clear()
    cfl.main(["--role", "server", "--bind", "127.0.0.1", "--peer", "127.0.0.2"])
    assert calls["ping"] == ("127.0.0.2", "127.0.0.1")
    assert calls["serve"][0] == "127.0.0.1"


@pytest.mark.parametrize("argv", [["--role", "server", "--soak", "5"], ["--role", "client", "--soak", "0"],
                                  ["--role", "client", "--soak", "-1"]])
def test_soak_argument_validation(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cfl.main(argv)
    assert exc.value.code == 2
    assert "--soak" in capsys.readouterr().err
