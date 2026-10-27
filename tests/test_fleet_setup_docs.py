"""Static checks for the fleet setup documentation and the fleet_env scripts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
    script = f'unset FLIGHTRISK_GROUND_LOCAL; export FLIGHTRISK_FLEET_NET={net}; {extra} source scripts/fleet_env.sh && echo "$FLIGHTRISK_EDGE_HOST $FLIGHTRISK_EDGE_WS $FLIGHTRISK_TELLO_HOST"'
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTRISK_")}
    return subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, check=False)


def test_fleet_env_sh_ap_and_station():
    ap = _source("ap")
    assert ap.returncode == 0
    assert ap.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://10.42.0.1:9000 192.168.10.1"

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


def test_thunderbolt_path_docs():
    fleet = (ROOT / "docs" / "fleet-setup.md").read_text(encoding="utf-8")
    assert "## Path 3: Thunderbolt link" in fleet
    for needle in ("Fallback ladder", "UNVERIFIED", "--soak", "10.42.0.2", "thunderbolt-link-results-template.md"):
        assert needle in fleet, needle
    windows = (ROOT / "docs" / "windows-edge-setup.md").read_text(encoding="utf-8")
    for needle in ("Thunderbolt link to the Mac", "New-NetIPAddress", "-NlMtuBytes 1500", "--soak"):
        assert needle in windows, needle
    assert (ROOT / "docs" / "thunderbolt-link-results-template.md").is_file()


def test_fleet_env_sh_ap_ground_local_uses_loopback():
    local = _source("ap", "export FLIGHTRISK_GROUND_LOCAL=1;")
    assert local.returncode == 0
    assert local.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://127.0.0.1:9000 192.168.10.1"
    # Only the exact value 1 selects loopback; anything else keeps the cable address.
    other = _source("ap", "export FLIGHTRISK_GROUND_LOCAL=yes;")
    assert other.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://10.42.0.1:9000 192.168.10.1"


def test_fleet_env_sh_warns_on_ambiguous_ground_local_values():
    for value in ("true", "yes", " 1", "1 "):
        result = _source("ap", f"export FLIGHTRISK_GROUND_LOCAL='{value}';")
        assert result.stdout.strip().splitlines()[-1].split()[1] == "ws://10.42.0.1:9000", value
        assert "only the exact value 1" in result.stderr, value
    for value in ("1", "0", ""):
        result = _source("ap", f"export FLIGHTRISK_GROUND_LOCAL='{value}';")
        assert "only the exact value 1" not in result.stderr, value


def test_unexported_ground_local_still_reaches_the_edge_launcher():
    # Regression: a plain (unexported) shell variable must not be lost when demo_edge.sh re-sources fleet_env.sh.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("FLIGHTRISK_", "DEMO_")) and k not in ("DRONE_ID", "ALT_CM")}
    env.update({"FLIGHTRISK_FLEET_NET": "ap", "FLIGHTRISK_EDGE_TOKEN": "test-token", "DEMO_DRY_RUN": "1"})
    script = (
        "FLIGHTRISK_GROUND_LOCAL=1; source scripts/fleet_env.sh >/dev/null && "
        'echo "parent ws=$FLIGHTRISK_EDGE_WS" && DRONE_ID=1 ALT_CM=100 scripts/demo_edge.sh'
    )
    result = subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "parent ws=ws://127.0.0.1:9000"
    assert "--ws-url ws://127.0.0.1:9000" in lines[-1], lines[-1]


def test_fleet_env_sh_station_ignores_ground_local():
    station = _source("station", "export FLIGHTRISK_GROUND_LOCAL=1 FLIGHTRISK_GROUND_IP=192.168.1.20 FLIGHTRISK_TELLO_HOST=192.168.1.50;")
    assert station.stdout.strip().splitlines()[-1] == "192.168.1.20 ws://192.168.1.20:9000 192.168.1.50"


def test_fleet_env_ap_never_binds_a_wildcard():
    for name in ("fleet_env.sh", "fleet_env.ps1"):
        assert "0.0.0.0" not in (ROOT / "scripts" / name).read_text(encoding="utf-8"), name


def _ps1(net: str, extra: str = "") -> subprocess.CompletedProcess:
    script = (
        f'$env:FLIGHTRISK_FLEET_NET = "{net}"; {extra} . ./scripts/fleet_env.ps1 *> $null; '
        'Write-Output "$($env:FLIGHTRISK_EDGE_HOST) $($env:FLIGHTRISK_EDGE_WS) $($env:FLIGHTRISK_TELLO_HOST)"'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTRISK_")}
    return subprocess.run(["pwsh", "-NoProfile", "-Command", script], cwd=ROOT, env=env, capture_output=True, text=True, check=False)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_fleet_env_ps1_ap_output():
    ap = _ps1("ap", '$env:FLIGHTRISK_GROUND_LOCAL = $null;')
    assert ap.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://10.42.0.1:9000 192.168.10.1"
    local = _ps1("ap", '$env:FLIGHTRISK_GROUND_LOCAL = "1";')
    assert local.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://127.0.0.1:9000 192.168.10.1"
    # Only the exact value 1 selects loopback.
    other = _ps1("ap", '$env:FLIGHTRISK_GROUND_LOCAL = "yes";')
    assert other.stdout.strip().splitlines()[-1] == "127.0.0.1,10.42.0.1 ws://10.42.0.1:9000 192.168.10.1"
    # Station mode ignores the variable.
    station = _ps1(
        "station",
        '$env:FLIGHTRISK_GROUND_LOCAL = "1"; $env:FLIGHTRISK_GROUND_IP = "192.168.1.20"; $env:FLIGHTRISK_TELLO_HOST = "192.168.1.50";',
    )
    assert station.stdout.strip().splitlines()[-1] == "192.168.1.20 ws://192.168.1.20:9000 192.168.1.50"


def test_fleet_env_ps1_ap_text_matches_sh():
    text = (ROOT / "scripts" / "fleet_env.ps1").read_text(encoding="utf-8")
    for needle in ('"127.0.0.1,10.42.0.1"', "FLIGHTRISK_GROUND_LOCAL", '"ws://127.0.0.1:9000"', '"ws://10.42.0.1:9000"'):
        assert needle in text, needle


def test_loopback_edge1_docs():
    fleet = (ROOT / "docs" / "fleet-setup.md").read_text(encoding="utf-8")
    for needle in (
        "127.0.0.1,10.42.0.1",
        "FLIGHTRISK_GROUND_LOCAL",
        "ws://127.0.0.1:9000",
        "ground_bind_address_lost",
        "ground_bind_address_restored",
    ):
        assert needle in fleet, needle
    assert "--ws-url ws://127.0.0.1:9000" in fleet
    assert "--drone-id drone-1 --ws-url ws://10.42.0.1:9000" not in fleet
    demo = (ROOT / "docs" / "demo-script.md").read_text(encoding="utf-8")
    assert "FLIGHTRISK_GROUND_LOCAL=1 DRONE_ID=1" in demo
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "FLIGHTRISK_GROUND_LOCAL=1 DRONE_ID=1" in readme
    # The AP fallback rung that runs a fake edge must not depend on the cable address.
    assert "export FLIGHTRISK_GROUND_LOCAL=1; source scripts/fleet_env.sh" in demo
    assert "needs the Mac's 10.42.0.1 address to stay up" not in demo
    assert "UNVERIFIED" in demo
    assert "lsof -nP -iTCP:9000 -sTCP:LISTEN" in fleet
    assert "export FLIGHTRISK_GROUND_LOCAL=1" in fleet
    assert "30 second cap" not in fleet
    assert "if 10.42.0.1 goes away" in fleet.lower()
