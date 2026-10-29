"""installer/windows: install.ps1, update.ps1, uninstall.ps1 and the Inno Setup script.

The scripts are written for Windows PowerShell 5.1 on Windows. Here they run under PowerShell 7
(``pwsh``) on macOS or Linux, skipped where ``pwsh`` is not installed: a parse check of every
script, the pure functions (tag selection, ref names, the status JSON, the install lock), and a
real install, update and uninstall into a temporary folder from a local fixture repository, with
FLIGHTRISK_INSTALL_SKIP_DEPS=1 and FLIGHTRISK_NO_SHORTCUTS=1. winget, the Start menu entry, the
registry entry and the Windows process check are not exercised here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.support.installer_repo import git, make_fixture_repo

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = ROOT / "installer" / "windows"
INSTALL = WINDOWS / "install.ps1"
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)

pytestmark = pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")


def pwsh(command: str, env: dict[str, str] | None = None, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", command], env=env or {**os.environ},
                          capture_output=True, text=True, timeout=timeout, check=False)


def lib(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run *snippet* after dot-sourcing install.ps1 as a library.

    FLIGHTRISK_HOME stands in for %LOCALAPPDATA%\\FlightRisk, which is not set outside Windows;
    the functions under test only compute paths from it.
    """
    env = {**(env or os.environ), "FLIGHTRISK_INSTALL_LIB_ONLY": "1"}
    env.setdefault("FLIGHTRISK_HOME", str(Path(tempfile.gettempdir()) / "flightrisk-lib-test"))
    return pwsh(f". '{INSTALL}'\n{snippet}", env=env)


def run_script(script: Path, *args: str, env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script), *args], env=env,
                          capture_output=True, text=True, timeout=timeout, check=False)


def test_every_script_parses():
    paths = sorted(WINDOWS.glob("*.ps1"))
    assert {p.name for p in paths} == {"install.ps1", "update.ps1", "uninstall.ps1"}
    for path in paths:
        result = pwsh("$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                      f"'{path}', [ref]$null, [ref]$errors); $errors | ForEach-Object {{ $_.ToString() }}")
        assert result.returncode == 0 and result.stdout.strip() == "", f"{path.name}: {result.stdout}{result.stderr}"


def test_script_analyzer_finds_nothing():
    probe = pwsh("if (Get-Module -ListAvailable PSScriptAnalyzer) { 'yes' } else { 'no' }")
    if probe.stdout.strip() != "yes":
        pytest.skip("PSScriptAnalyzer not installed")
    result = pwsh(f"Invoke-ScriptAnalyzer -Path '{WINDOWS}' -Settings '{WINDOWS / 'PSScriptAnalyzerSettings.psd1'}' "
                  "| ForEach-Object { \"$($_.ScriptName):$($_.Line) $($_.RuleName) $($_.Message)\" }", timeout=180)
    assert result.stdout.strip() == "", result.stdout


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["v0.1.0", "v0.2.0", "v0.10.0", "v0.9.9"], "v0.10.0"),
        (["v1.0.0", "v1.0.0-rc1", "v2.0.0-beta", "hackathon/kickoff"], "v1.0.0"),
        (["v2.0.0", "v10.0.0", "v9.99.99"], "v10.0.0"),
        (["hackathon/kickoff", "1.2.3"], ""),
    ],
)
def test_select_latest_tag(tags, expected):
    lines = ",".join(f"\"{'ab' * 20}`trefs/tags/{tag}\"" for tag in tags)
    result = lib(f"$t = Select-LatestTag @({lines}); if ($t) {{ $t }} else {{ '<none>' }}")
    assert result.stdout.strip() == (expected or "<none>"), result.stderr


@pytest.mark.parametrize(("ref", "ok"), [("v1.2.3", True), ("main", True), ("v1.2", False), ("Main", False),
                                         ("v1.2.3-rc1", False), ("v1.2.3; Remove-Item", False)])
def test_ref_names(ref, ok):
    env = {**os.environ, "REF_TO_CHECK": ref}
    assert lib("Test-RefName $env:REF_TO_CHECK", env=env).stdout.strip() == ("True" if ok else "False")


def test_status_file_is_json_without_bom(tmp_path):
    status = tmp_path / "status.json"
    message = 'quote " backslash \\ newline \n tab \t done'
    env = {**os.environ, "FLIGHTRISK_STATUS_FILE": str(status), "FLIGHTRISK_HOME": str(tmp_path), "MESSAGE": message}
    result = lib('$script:Step = "packages"; $script:Ref = "v1.2.3"; Write-Status "error" $env:MESSAGE', env=env)
    assert result.returncode == 0, result.stderr
    raw = status.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    data = json.loads(raw)
    assert list(data) == ["state", "step", "message", "ref", "pid", "at"]
    assert data["state"] == "error" and data["step"] == "packages" and data["ref"] == "v1.2.3"
    assert data["message"] == message
    assert isinstance(data["pid"], int)


def test_install_lock_is_exclusive(tmp_path):
    env = {**os.environ, "FLIGHTRISK_STATUS_FILE": str(tmp_path / "status.json"), "FLIGHTRISK_HOME": str(tmp_path),
           "FLIGHTRISK_INSTALL_LIB_ONLY": "1"}
    ready = tmp_path / "held"
    hold = (f". '{INSTALL}'; if (Enter-InstallLock) {{ New-Item -ItemType File '{ready}' | Out-Null; "
            "Start-Sleep -Seconds 30 }")
    holder = subprocess.Popen(["pwsh", "-NoProfile", "-NonInteractive", "-Command", hold], env=env)
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.2)
        assert ready.exists()
        assert lib("Enter-InstallLock", env=env).stdout.strip() == "False"
    finally:
        holder.kill()
        holder.wait()
    assert lib("Enter-InstallLock", env=env).stdout.strip() == "True"


class WinBox:
    def __init__(self, tmp_path: Path):
        self.repo = make_fixture_repo(tmp_path / "repo")
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.flightrisk_home = tmp_path / "LocalAppData" / "FlightRisk"
        self.app_dir = self.flightrisk_home / "app"

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home), "PATH": os.environ["PATH"], "LANG": "en_US.UTF-8",
            "FLIGHTRISK_HOME": str(self.flightrisk_home), "FLIGHTRISK_REPO_URL": str(self.repo),
            "FLIGHTRISK_INSTALL_SKIP_DEPS": "1", "FLIGHTRISK_NO_SHORTCUTS": "1", "FLIGHTRISK_PYTHON": BASE_PYTHON,
            # An index nothing listens on: no test may ever download packages from PyPI.
            "FLIGHTRISK_PIP_INDEX_URL": "http://127.0.0.1:9/simple",
        }
        env.update(extra)
        return env

    def run(self, script: Path, *args: str, **extra: str) -> subprocess.CompletedProcess:
        return run_script(script, *args, env=self.env(**extra))

    def shim_git(self, on: str, action: str) -> None:
        """Put a git in front of the real one that, for the subcommand *on*, first runs *action* (bash)."""
        shims = self.home / "shims"
        shims.mkdir(exist_ok=True)
        real = shutil.which("git", path="/usr/bin:/bin")
        (shims / "git").write_text(
            f'#!/bin/bash\nREAL_GIT="{real}"\nfor arg in "$@"; do\n  if [ "$arg" = "{on}" ]; then\n'
            f'    {action}\n  fi\ndone\nexec "$REAL_GIT" "$@"\n')
        (shims / "git").chmod(0o755)

    def shimmed_env(self) -> dict[str, str]:
        return {"PATH": f"{self.home / 'shims'}:{os.environ['PATH']}"}

    def head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.app_dir)

    def tag(self, name: str) -> str:
        return git("rev-parse", f"{name}^{{commit}}", cwd=self.repo)


@pytest.fixture
def winbox(tmp_path: Path) -> WinBox:
    box = WinBox(tmp_path)
    result = box.run(INSTALL)
    assert result.returncode == 0, result.stdout + result.stderr
    return box


def test_install_into_a_temporary_folder(winbox: WinBox):
    assert winbox.head() == winbox.tag("v0.2.0")
    marker = (winbox.app_dir / ".git" / "flightrisk-installer").read_text()
    assert "ref=v0.2.0" in marker and "installer=installer/windows/install.ps1" in marker
    assert (winbox.app_dir / "venv" / "bin" / "python").exists()
    status = json.loads((winbox.flightrisk_home / "install-status.json").read_text())
    assert status["state"] == "done" and status["ref"] == "v0.2.0"
    again = winbox.run(INSTALL)
    assert again.returncode == 0
    assert "already at v0.2.0" in again.stdout


def test_install_never_moves_backwards_and_refuses_local_changes(winbox: WinBox):
    result = winbox.run(INSTALL, FLIGHTRISK_REF="v0.1.0")
    assert result.returncode == 0
    assert "the installed version is newer" in result.stdout
    assert winbox.head() == winbox.tag("v0.2.0")
    (winbox.app_dir / "requirements.txt").write_text("changed\n")
    result = winbox.run(INSTALL)
    assert result.returncode == 1
    assert "has local changes" in result.stdout
    assert json.loads((winbox.flightrisk_home / "install-status.json").read_text())["state"] == "error"


def test_install_refuses_a_folder_it_did_not_make(tmp_path):
    box = WinBox(tmp_path)
    box.flightrisk_home.mkdir(parents=True)
    git("clone", "-q", str(box.repo), str(box.app_dir), cwd=tmp_path)
    result = box.run(INSTALL)
    assert result.returncode == 1
    assert "was not made by this installer" in result.stdout
    assert not (box.app_dir / "venv").exists()


def test_bad_ref_is_refused(tmp_path):
    box = WinBox(tmp_path)
    result = box.run(INSTALL, FLIGHTRISK_REF="v1; Remove-Item x")
    assert result.returncode == 1
    assert "must be a release tag" in result.stdout
    assert not box.app_dir.exists()


def test_update_check_and_update(winbox: WinBox):
    update = winbox.app_dir / "installer" / "windows" / "update.ps1"
    data = json.loads(winbox.run(update, "-Check").stdout)
    assert data == {"installDir": str(winbox.app_dir), "installed": "v0.2.0", "latest": "v0.2.0",
                    "updateAvailable": False}
    git("commit", "-q", "--allow-empty", "-m", "third", cwd=winbox.repo)
    git("tag", "v0.3.0", cwd=winbox.repo)
    assert json.loads(winbox.run(update, "-Check").stdout)["updateAvailable"] is True
    result = winbox.run(update)
    assert result.returncode == 0, result.stdout + result.stderr
    assert winbox.head() == winbox.tag("v0.3.0")


def test_uninstall_check_refusals_and_removal(winbox: WinBox):
    uninstall = winbox.app_dir / "installer" / "windows" / "uninstall.ps1"
    copy = winbox.home / "uninstall.ps1"
    shutil.copy2(uninstall, copy)
    (winbox.app_dir / "logs").mkdir()
    (winbox.app_dir / "logs" / "edge.log").write_text("line")
    token_file = winbox.home / ".flightrisk-fleet.env"
    token_file.write_text("export FLIGHTRISK_EDGE_TOKEN=not-a-real-token\n")

    check = json.loads(winbox.run(copy, "-Check").stdout)
    assert check["exists"] and check["flightriskCheckout"] and check["installerMade"]
    assert check["running"] == [] and check["unsaved"] == [] and check["keep"] == ["logs"]

    marker = winbox.app_dir / ".git" / "flightrisk-installer"
    saved = marker.read_text()
    marker.unlink()
    refused = winbox.run(copy, "-Yes", "-Force")
    assert refused.returncode == 2
    assert '"reason":"not-made-by-installer"' in refused.stdout
    marker.write_text(saved)

    (winbox.app_dir / "local.txt").write_text("x")
    git("add", "local.txt", cwd=winbox.app_dir)
    git("commit", "-qm", "local", cwd=winbox.app_dir)
    refused = winbox.run(copy, "-Yes")
    assert refused.returncode == 4
    assert '"reason":"unsaved-work"' in refused.stdout

    removed = winbox.run(copy, "-Yes", "-Force")
    assert removed.returncode == 0, removed.stdout + removed.stderr
    data = json.loads(removed.stdout.strip().splitlines()[0])
    assert data["removed"] is True
    assert not winbox.app_dir.exists()
    assert not winbox.flightrisk_home.exists()
    kept = Path(data["keptData"])
    assert kept.parent == winbox.flightrisk_home.parent / "FlightRisk data", "not Documents, which OneDrive may sync"
    assert (kept / "logs" / "edge.log").read_text() == "line"
    assert token_file.read_text() == "export FLIGHTRISK_EDGE_TOKEN=not-a-real-token\n"
    assert "not-a-real-token" not in removed.stdout + removed.stderr


@pytest.mark.parametrize("how", ["venv-python", "renamed-in-folder"])
def test_uninstall_and_update_refuse_while_flightrisk_runs(winbox: WinBox, how):
    if how == "venv-python":
        proc = subprocess.Popen([str(winbox.app_dir / "venv" / "bin" / "python"), "-c", "import time; time.sleep(60)"])
    else:
        # A Python whose command line does not name the install, started in the install folder.
        proc = subprocess.Popen(["/bin/bash", "-c", "exec -a /x/Python.app/Contents/MacOS/Python sleep 60"],
                                cwd=winbox.app_dir)
    time.sleep(0.5)
    try:
        result = winbox.run(winbox.app_dir / "installer" / "windows" / "uninstall.ps1", "-Yes", "-Force")
        assert result.returncode == 3, result.stdout + result.stderr
        assert '"reason":"running"' in result.stdout
        git("commit", "-q", "--allow-empty", "-m", "third", cwd=winbox.repo)
        git("tag", "v0.3.0", cwd=winbox.repo)
        update = winbox.run(INSTALL)
        assert update.returncode == 1
        assert "FlightRisk is running" in update.stdout
        assert winbox.head() == winbox.tag("v0.2.0")
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_shared_functions_are_the_same_in_install_and_uninstall():
    def block(path: Path) -> str:
        text = path.read_text()
        return text[text.index("# --- shared with uninstall.ps1: begin"):text.index("# --- shared with uninstall.ps1: end")]

    assert block(INSTALL) == block(WINDOWS / "uninstall.ps1")


def test_uninstall_refuses_while_an_install_runs(winbox: WinBox):
    ready = winbox.home / "held"
    env = {**winbox.env(), "FLIGHTRISK_INSTALL_LIB_ONLY": "1"}
    hold = (f". '{INSTALL}'; if (Enter-InstallLock) {{ New-Item -ItemType File '{ready}' | Out-Null; "
            "Start-Sleep -Seconds 30 }")
    holder = subprocess.Popen(["pwsh", "-NoProfile", "-NonInteractive", "-Command", hold], env=env)
    try:
        deadline = time.time() + 30
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.2)
        result = winbox.run(winbox.app_dir / "installer" / "windows" / "uninstall.ps1", "-Yes", "-Force")
        assert result.returncode == 3
        assert '"reason":"install-running"' in result.stdout
        assert winbox.app_dir.exists()
        # Under "irm ... | iex" a busy lock must not close the PowerShell window: it throws instead of exiting.
        script = f"try {{ Invoke-Expression (Get-Content -Raw '{INSTALL}') }} catch {{ 'caught' }}; 'window still open'"
        busy = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", script], env=winbox.env(),
                              capture_output=True, text=True, timeout=60, check=False)
        assert "Another FlightRisk install is running" in busy.stdout
        assert "window still open" in busy.stdout
    finally:
        holder.kill()
        holder.wait()


def test_a_first_install_killed_mid_download_can_be_repaired(tmp_path):
    box = WinBox(tmp_path)
    box.shim_git("clone", '"$REAL_GIT" "$@"; kill -KILL $PPID; exit 1')
    first = box.run(INSTALL, **box.shimmed_env())
    assert first.returncode != 0
    assert not box.app_dir.exists(), "no half-made install where the Start menu entry looks for one"
    assert list(box.flightrisk_home.glob(".app-incoming.*"))
    again = box.run(INSTALL)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "left by an interrupted install" in again.stdout
    assert not list(box.flightrisk_home.glob(".app-incoming.*"))
    assert box.head() == box.tag("v0.2.0")


def test_an_update_killed_mid_checkout_is_finished_by_the_next_run(winbox: WinBox):
    git("commit", "-q", "--allow-empty", "-m", "third", cwd=winbox.repo)
    git("tag", "v0.3.0", cwd=winbox.repo)
    winbox.shim_git("checkout", f'echo half-written >> "{winbox.app_dir / "requirements.txt"}"; kill -KILL $PPID; exit 1')
    first = winbox.run(INSTALL, **winbox.shimmed_env())
    assert first.returncode != 0
    assert "pending=" in (winbox.app_dir / ".git" / "flightrisk-installer").read_text()
    again = winbox.run(INSTALL)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "an earlier update was interrupted" in again.stdout
    assert winbox.head() == winbox.tag("v0.3.0")
    assert git("status", "--porcelain", "--untracked-files=no", cwd=winbox.app_dir) == ""


def test_update_stops_when_the_releases_cannot_be_read(winbox: WinBox):
    update = winbox.app_dir / "installer" / "windows" / "update.ps1"
    result = winbox.run(update, FLIGHTRISK_REPO_URL=str(winbox.home / "no-such-repo"))
    assert result.returncode == 1
    assert "could not reach" in result.stdout
    assert winbox.head() == winbox.tag("v0.2.0")


def test_shared_folders_are_refused(winbox: WinBox):
    install = winbox.run(INSTALL, FLIGHTRISK_HOME=str(winbox.home))
    assert install.returncode == 1 and "is a shared folder" in install.stdout
    assert not (winbox.home / "app").exists()
    uninstall = winbox.run(winbox.app_dir / "installer" / "windows" / "uninstall.ps1", "-Yes",
                           FLIGHTRISK_HOME=str(winbox.home))
    assert uninstall.returncode == 2 and '"reason":"unsafe-folder"' in uninstall.stdout


def test_a_failed_package_install_leaves_no_stamp(winbox: WinBox):
    stamp = winbox.app_dir / "venv" / ".flightrisk-deps"
    stamp.write_text("requirements-edge.txt old\n")
    # pip ignores PIP_* settings here (--isolated); the dead index makes it fail at once.
    result = winbox.run(INSTALL, FLIGHTRISK_INSTALL_SKIP_DEPS="", PIP_INDEX_URL="https://pypi.org/simple")
    assert result.returncode == 1
    assert not stamp.exists(), "half-installed packages must not count as installed"


def test_inno_setup_script_is_per_user_and_points_at_real_files():
    iss = (WINDOWS / "FlightRisk.iss").read_text()
    assert "PrivilegesRequired=lowest" in iss
    assert "Uninstallable=no" in iss, "install.ps1 registers the Installed apps entry itself"
    assert (WINDOWS / "install.ps1").is_file() and 'Source: "install.ps1"' in iss
    assert "SetupIconFile=..\\assets\\FlightRisk.ico" in iss
    assert (ROOT / "installer" / "assets" / "FlightRisk.ico").is_file()
    assert "-ExecutionPolicy Bypass -File" in iss
