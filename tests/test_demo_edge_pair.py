"""scripts/demo_edge_pair.ps1: two edges on one Windows laptop, one per Wi-Fi adapter.

Runs the script with PowerShell (skipped where ``pwsh`` is not installed) in
dry-run and child modes only, so nothing is started and no packet is sent.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "demo_edge_pair.ps1"
TOKEN = "test-token-that-must-never-be-printed"

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")


def _run(*args: str, token: str | None = TOKEN) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTRISK_")}
    if token is not None:
        env["FLIGHTRISK_EDGE_TOKEN"] = token
    return subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(SCRIPT), *args],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=60,
    )


def _plain(text: str) -> str:
    """PowerShell error text without colour codes or its line wrapping, on one line."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"\s*\|\s*", " ", text)
    return " ".join(text.split())


def test_dry_run_prints_both_edges_with_ips_slots_and_no_token():
    result = _run("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-IfIndex1", "12", "-IfIndex2", "23",
                  "-DryRun")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 2
    assert "--tello-local-ip 192.168.10.2 --tello-ifindex 12 --drone-id drone-1 --alt-slot 50" in lines[0]
    assert "--tello-local-ip 192.168.10.50 --tello-ifindex 23 --drone-id drone-2 --alt-slot 80" in lines[1]
    for line in lines:
        assert "--source tello --tello-host 192.168.10.1" in line
        assert "--ws-url ws://10.42.0.1:9000" in line
    assert TOKEN not in result.stdout + result.stderr


def test_without_ifindex_no_ifindex_flag_is_passed():
    result = _run("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-DryRun")
    assert result.returncode == 0, result.stderr
    assert "--tello-ifindex" not in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.2"), "both 192.168.10.2"),
        (("-LocalIp1", "0.0.0.0", "-LocalIp2", "192.168.10.50"), "IPv4 address of one Wi-Fi adapter"),
        (("-LocalIp1", "192.168.10.2", "-LocalIp2", "wifi"), "IPv4 address of one Wi-Fi adapter"),
        (("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-DroneId2", "drone-1"), "different drone ids"),
        (("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-Slot1", "5"), "at least 10"),
        (("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-IfIndex1", "7", "-IfIndex2", "7"),
         "each adapter has its own index"),
    ],
)
def test_bad_parameters_are_refused(args, message):
    result = _run(*args, "-DryRun")
    assert result.returncode != 0
    assert message in _plain(result.stdout + result.stderr)


def test_missing_token_is_refused():
    result = _run("-LocalIp1", "192.168.10.2", "-LocalIp2", "192.168.10.50", "-DryRun", token=None)
    assert result.returncode != 0
    assert "FLIGHTRISK_EDGE_TOKEN is not set" in _plain(result.stderr)


@pytest.mark.skipif(shutil.which("echo") is None, reason="no echo program")
def test_child_mode_runs_one_edge_with_its_adapter():
    echo = shutil.which("echo")
    result = _run("-Child", "-LocalIp", "127.0.0.1", "-DroneId", "drone-2", "-Slot", "80", "-Python", echo)
    assert result.returncode == 0, result.stderr
    assert ("-m flightrisk.edge_device --source tello --tello-host 192.168.10.1 --tello-local-ip 127.0.0.1 "
            "--drone-id drone-2 --alt-slot 80 --ws-url ws://10.42.0.1:9000") in result.stdout
    assert TOKEN not in result.stdout + result.stderr


def test_script_turns_off_quickedit_and_never_binds_a_wildcard():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "ENABLE_QUICK_EDIT_MODE" in text and "SetConsoleMode" in text
    assert "FLIGHTRISK_EDGE_TOKEN" in text
    assert "$env:FLIGHTRISK_EDGE_TOKEN\"" not in text  # never interpolated into output


@pytest.mark.skipif(shutil.which("echo") is None, reason="no echo program")
def test_child_mode_tees_the_edge_output_to_its_log_file(tmp_path):
    log = tmp_path / "edge-drone-1.log"
    result = _run("-Child", "-LocalIp", "127.0.0.1", "-DroneId", "drone-1", "-Slot", "50",
                  "-Python", shutil.which("echo"), "-LogFile", str(log))
    assert result.returncode == 0, result.stderr
    assert "--tello-local-ip 127.0.0.1 --drone-id drone-1 --alt-slot 50" in log.read_text(encoding="utf-8")
    assert TOKEN not in log.read_text(encoding="utf-8")


def test_child_mode_keeps_running_the_edge_after_an_error_line():
    """Windows PowerShell 5.1 turns redirected stderr lines into errors; they must not stop the window's script."""
    text = SCRIPT.read_text(encoding="utf-8")
    child = text[text.index('if ($PSCmdlet.ParameterSetName -eq "Child")'):]
    assert child.index('$ErrorActionPreference = "Continue"') < child.index("& $Python @edgeArgs")
    assert '$env:PYTHONUNBUFFERED = "1"' in child
