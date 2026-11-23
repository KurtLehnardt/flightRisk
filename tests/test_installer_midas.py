"""The installers' MiDaS step: run the pinned, verifying install; warn when offline; stop when refused.

The step runs ``python -m flightrisk.drone.midas_model install`` with the install's venv
Python, from the app folder. A stand-in venv Python records how it was called and exits
with the code the real module uses (0 installed, 2 download failed, 1 refused), so no
network and no torch are needed. Windows' install.ps1 runs under PowerShell 7 and is
skipped where ``pwsh`` is not installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WINDOWS_INSTALL = ROOT / "installer" / "windows" / "install.ps1"
MACOS_INSTALL = ROOT / "installer" / "macos" / "install.sh"
EXPECTED_ARGS = "-E -s -m flightrisk.drone.midas_model install"


def _fake_venv_python(app_dir: Path, exit_code: int) -> Path:
    """A venv Python that logs "cwd|args" and exits with *exit_code*; returns the log path."""
    python = app_dir / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    calls = app_dir.parent / "python-calls.txt"
    python.write_text(f'#!/bin/bash\necho "$PWD|$*" >> "{calls}"\nexit {exit_code}\n')
    python.chmod(0o755)
    return calls


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = {**os.environ, "FLIGHTRISK_HOME": str(home), "FLIGHTRISK_INSTALL_LIB_ONLY": "1", **extra}
    env.pop("FLIGHTRISK_INSTALL_SKIP_DEPS", None)
    env.update(extra)
    return env


def _macos(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    script = f'source "{MACOS_INSTALL}"\nset +e\n{snippet}\n'
    return subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)


def _windows(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", f". '{WINDOWS_INSTALL}'\n{snippet}"],
                          env=env, capture_output=True, text=True, timeout=60, check=False)


RUNNERS = {
    "macos": (_macos, "install_midas_weights; echo AFTER_STEP"),
    "windows": (_windows, 'Get-MidasWeights; Write-Host "AFTER_STEP"'),
}


def _skip_without_pwsh(platform: str) -> None:
    if platform == "windows" and shutil.which("pwsh") is None:
        pytest.skip("PowerShell not installed")
    if platform == "macos" and not os.path.exists("/bin/bash"):
        pytest.skip("/bin/bash not present (the macOS installer is sourced by bash)")


@pytest.mark.parametrize("platform", sorted(RUNNERS))
def test_installs_with_the_venv_python_from_the_app_folder(platform, tmp_path):
    _skip_without_pwsh(platform)
    app = tmp_path / "fr" / "app"
    calls = _fake_venv_python(app, 0)
    run, snippet = RUNNERS[platform]
    result = run(snippet, _env(tmp_path / "fr"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "MiDaS depth weights downloaded and checked" in result.stdout
    assert "AFTER_STEP" in result.stdout
    cwd, args = calls.read_text(encoding="utf-8").strip().split("|")
    assert Path(cwd).resolve() == app.resolve()
    assert args == EXPECTED_ARGS


@pytest.mark.parametrize("platform", sorted(RUNNERS))
def test_an_offline_install_warns_and_goes_on(platform, tmp_path):
    _skip_without_pwsh(platform)
    _fake_venv_python(tmp_path / "fr" / "app", 2)
    run, snippet = RUNNERS[platform]
    result = run(snippet, _env(tmp_path / "fr"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "run the installer again before flying" in result.stdout
    assert "AFTER_STEP" in result.stdout


@pytest.mark.parametrize("platform", sorted(RUNNERS))
def test_refused_weights_stop_the_install(platform, tmp_path):
    _skip_without_pwsh(platform)
    _fake_venv_python(tmp_path / "fr" / "app", 1)
    run, snippet = RUNNERS[platform]
    result = run(snippet, _env(tmp_path / "fr"))
    assert result.returncode == 1
    assert "could not be installed and checked" in result.stdout + result.stderr
    assert "AFTER_STEP" not in result.stdout


@pytest.mark.parametrize("platform", sorted(RUNNERS))
def test_skip_deps_skips_the_step(platform, tmp_path):
    _skip_without_pwsh(platform)
    calls = _fake_venv_python(tmp_path / "fr" / "app", 1)
    run, snippet = RUNNERS[platform]
    result = run(snippet, _env(tmp_path / "fr", FLIGHTRISK_INSTALL_SKIP_DEPS="1"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()


def test_both_installers_run_the_step_after_the_detector_weights():
    mac = MACOS_INSTALL.read_text(encoding="utf-8")
    main = mac[mac.index("\nmain() {"):]
    assert main.index("download_weights") < main.index("install_midas_weights") < main.index("create_app")
    win = WINDOWS_INSTALL.read_text(encoding="utf-8")
    flow = win[win.index("function Invoke-Install {"):]
    assert flow.index("Get-Weights") < flow.index("Get-MidasWeights") < flow.index("Register-App")


def test_developer_scripts_install_the_weights_too():
    for name in ("install_linux.sh", "install_macos.sh", "install_windows.ps1"):
        assert "-m flightrisk.drone.midas_model install" in (ROOT / "scripts" / name).read_text(encoding="utf-8"), name
