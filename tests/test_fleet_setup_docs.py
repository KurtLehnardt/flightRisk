"""Static checks for the fleet setup documentation and the fleet_env scripts."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_fleet_setup_doc_sections():
    text = (ROOT / "docs" / "fleet-setup.md").read_text(encoding="utf-8")
    for needle in (
        "10.42.0.1",
        "10.42.0.2",
        "New-NetIPAddress",
        "Set-NetConnectionProfile",
        "New-NetFirewallRule",
        "networksetup",
        "ifconfig",
        "FLIGHTRISK_FLEET_NET",
        "FLIGHTRISK_EDGE_TOKEN",
        "port 8890",
        "--i-understand-this-reconfigures-the-drone",
    ):
        assert needle in text, needle
    assert "5 s" in text or "5 seconds" in text


def test_fleet_env_scripts_have_both_branches():
    for name in ("fleet_env.sh", "fleet_env.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        for needle in ("station", "ap", "10.42.0.1", "192.168.10.1", "FLIGHTRISK_EDGE_HOST", "FLIGHTRISK_EDGE_WS"):
            assert needle in text, (name, needle)


def _source(net: str, extra: str = "") -> subprocess.CompletedProcess:
    script = f'export FLIGHTRISK_FLEET_NET={net}; {extra} source scripts/fleet_env.sh && echo "$FLIGHTRISK_EDGE_HOST $FLIGHTRISK_EDGE_WS $FLIGHTRISK_TELLO_HOST"'
    return subprocess.run(["bash", "-c", script], cwd=ROOT, capture_output=True, text=True)


def test_fleet_env_sh_ap_and_station():
    ap = _source("ap")
    assert ap.returncode == 0
    assert ap.stdout.strip().splitlines()[-1] == "10.42.0.1 ws://10.42.0.1:9000 192.168.10.1"

    station = _source("station", "export FLIGHTRISK_GROUND_IP=192.168.1.20 FLIGHTRISK_TELLO_HOST=192.168.1.50;")
    assert station.returncode == 0
    assert station.stdout.strip().splitlines()[-1] == "192.168.1.20 ws://192.168.1.20:9000 192.168.1.50"

    assert _source("station").returncode != 0
    assert _source("bogus").returncode != 0


def test_check_fleet_link_help():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_fleet_link.py"), "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--role" in result.stdout
