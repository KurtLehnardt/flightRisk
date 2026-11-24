"""Unit tests for scripts/sitl_fleet.sh: argument handling and dry runs only, no vehicle is ever started."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the SITL fleet harness is a bash script")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sitl_fleet.sh"


def run(args: list[str], run_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, FLIGHTRISK_SITL_RUN_DIR=str(run_dir))
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=60, check=False
    )


def instances(out: str) -> list[dict[str, str]]:
    rows = []
    for line in out.splitlines():
        if line.startswith("instance "):
            rows.append(dict(part.split("=", 1) for part in line.split()[2:]))
    return rows


def test_script_is_valid_bash() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0


def test_defaults_files_hold_only_the_stream_rates() -> None:
    sitl = ROOT / "tests" / "integration" / "sitl"
    for version, prefix in (("4.6", "SR0_"), ("4.7", "MAV1_")):
        lines = (sitl / f"arducopter-{version}.parm").read_text().splitlines()
        assert lines == [f"{prefix}{name}" for name in ("EXT_STAT 2", "EXTRA1 4", "EXTRA2 4", "EXTRA3 2", "POSITION 4")]


def test_dry_run_ardupilot_eight_instances(tmp_path: Path) -> None:
    result = run(["up", "--autopilot", "ardupilot", "--version", "4.7.3", "--count", "8", "--base-instance", "0",
                  "--dry-run"], tmp_path / "run")
    assert result.returncode == 0, result.stderr
    rows = instances(result.stdout)
    assert [int(r["port"]) for r in rows] == list(range(5760, 5831, 10))
    assert [r["sysid"] for r in rows] == [str(n) for n in range(1, 9)]
    assert len({r["home"] for r in rows}) == 8
    assert [r["address"] for r in rows][0] == "tcpout://127.0.0.1:5760"
    assert re.search(r"^extra defaults: .*arducopter-4\.7\.parm$", result.stdout, re.M)
    assert "export FLIGHTRISK_SITL_SYSIDS=1,2,3,4,5,6,7,8" in result.stdout
    assert "export FLIGHTRISK_SITL_AUTOPILOT=ardupilot" in result.stdout
    assert "export FLIGHTRISK_SITL_VERSION=4.7.3" in result.stdout


def test_dry_run_picks_defaults_by_major_minor(tmp_path: Path) -> None:
    result = run(["up", "--autopilot", "ardupilot", "--version", "4.6.3", "--count", "2", "--dry-run"], tmp_path / "run")
    assert result.returncode == 0, result.stderr
    assert re.search(r"^extra defaults: .*arducopter-4\.6\.parm$", result.stdout, re.M)


def test_same_sysid_gives_one_everywhere(tmp_path: Path) -> None:
    result = run(["up", "--autopilot", "ardupilot", "--count", "8", "--same-sysid", "--dry-run"], tmp_path / "run")
    assert result.returncode == 0, result.stderr
    assert [r["sysid"] for r in instances(result.stdout)] == ["1"] * 8


def test_dry_run_px4_two_instances(tmp_path: Path) -> None:
    result = run(["up", "--autopilot", "px4", "--count", "2", "--dry-run"], tmp_path / "run")
    assert result.returncode == 0, result.stderr
    rows = instances(result.stdout)
    assert [r["address"] for r in rows] == ["udpin://0.0.0.0:14540", "udpin://0.0.0.0:14541"]
    assert [r["sysid"] for r in rows] == ["1", "2"]
    assert "export FLIGHTRISK_SITL_ADDRESS_2=udpin://0.0.0.0:14541" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["up", "--autopilot", "px4", "--base-instance", "9", "--count", "2", "--dry-run"],
        ["up", "--autopilot", "px4", "--count", "0", "--dry-run"],
        ["up", "--autopilot", "px4", "--count", "9", "--dry-run"],
        ["up", "--autopilot", "ardupilot", "--version", "9.9.9", "--dry-run"],
        ["up", "--autopilot", "px4", "--version", "4.7.3", "--dry-run"],
        ["up", "--autopilot", "plane", "--dry-run"],
        ["up", "--dry-run"],
        ["up", "--autopilot", "px4", "--bogus"],
        ["frobnicate"],
    ],
)
def test_bad_arguments_exit_two(tmp_path: Path, args: list[str]) -> None:
    run_dir = tmp_path / "run"
    result = run(args, run_dir)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not run_dir.exists()


def test_dry_run_starts_nothing_and_writes_no_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    before = set(tmp_path.iterdir())
    result = run(["up", "--autopilot", "ardupilot", "--count", "3", "--dry-run"], run_dir)
    assert result.returncode == 0, result.stderr
    assert not run_dir.exists()
    assert set(tmp_path.iterdir()) == before


def test_down_and_status_on_an_empty_run_directory(tmp_path: Path) -> None:
    down = run(["down", "--base-instance", "0"], tmp_path / "run")
    assert down.returncode == 0 and "SITL fleet down" in down.stdout
    status = run(["status"], tmp_path / "run")
    assert status.returncode == 0 and "running" not in status.stdout


def test_gate_dry_run_lists_files_and_starts_nothing(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    result = run(["gate", "--autopilot", "px4", "--files", "tests/integration/test_sitl.py,tests/integration/test_x.py",
                  "--dry-run"], run_dir)
    assert result.returncode == 0, result.stderr
    assert "GATE would run: tests/integration/test_sitl.py" in result.stdout
    assert "GATE would run: tests/integration/test_x.py" in result.stdout
    assert not run_dir.exists()
