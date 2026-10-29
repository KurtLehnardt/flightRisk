"""installer/macos: the per-user installer, FlightRisk.app, the .dmg, update and uninstall.

Every run uses a temporary HOME, install folder and Applications folder, and installs from a
local fixture repository with release tags, never from GitHub. Homebrew is a fake that records its
calls and fails, so nothing is installed on this Mac; FLIGHTRISK_INSTALL_SKIP_DEPS=1 replaces the
several-GB package install. The launcher started through FlightRisk.app listens on a free port and
is quit at the end of each test.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import site
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from tests.support.installer_repo import git as _git
from tests.support.installer_repo import make_fixture_repo

ROOT = Path(__file__).resolve().parent.parent
MACOS = ROOT / "installer" / "macos"
INSTALL = MACOS / "install.sh"
BUNDLE_ID = "io.github.kurtlehnardt.flightrisk.launcher"
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the macOS installer runs on macOS only")

class Box:
    """A temporary HOME with fake Homebrew, and the paths the installer uses inside it."""

    def __init__(self, root: Path, repo: Path):
        self.root = root
        self.repo = repo
        self.home = root / "home"
        self.shims = root / "shims"
        self.home.mkdir(parents=True)
        self.shims.mkdir()
        self.flightrisk_home = self.home / "Library" / "Application Support" / "FlightRisk"
        self.app_dir = self.flightrisk_home / "app"
        self.venv = self.app_dir / "venv"
        self.applications = self.home / "Applications"
        self.bundle = self.applications / "FlightRisk.app"
        self.status = self.flightrisk_home / "install-status.json"
        self.brew_log = self.home / "brew-calls.log"
        brew = self.shims / "brew"
        brew.write_text('#!/bin/sh\necho "brew $*" >> "$HOME/brew-calls.log"\nexit 1\n')
        brew.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.shims}:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": str(self.root),
            "LANG": "en_US.UTF-8",
            "FLIGHTRISK_REPO_URL": str(self.repo),
            "FLIGHTRISK_INSTALL_SKIP_DEPS": "1",
            "FLIGHTRISK_PYTHON": BASE_PYTHON,
            "FLIGHTRISK_BREW_CANDIDATES": "",
        }
        env.update(extra)
        return env

    def run(self, script: Path, *args: str, timeout: float = 120, **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(script), *args], env=self.env(**extra), cwd=self.root,
                              capture_output=True, text=True, timeout=timeout, check=False)

    def install(self, **extra: str) -> subprocess.CompletedProcess:
        return self.run(INSTALL, **extra)

    def status_json(self) -> dict:
        return json.loads(self.status.read_text())

    def head(self) -> str:
        return _git("rev-parse", "HEAD", cwd=self.app_dir)

    def tag_commit(self, tag: str) -> str:
        return _git("rev-parse", f"{tag}^{{commit}}", cwd=self.repo)

    def brew_calls(self) -> list[str]:
        return self.brew_log.read_text().splitlines() if self.brew_log.exists() else []


@pytest.fixture
def box(tmp_path: Path) -> Box:
    repo = make_fixture_repo(tmp_path / "repo")
    return Box(tmp_path / "box", repo)


@pytest.fixture
def installed(box: Box) -> Box:
    result = box.install()
    assert result.returncode == 0, result.stdout + result.stderr
    return box


def lib(snippet: str, env: dict[str, str] | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    """Run *snippet* in bash after sourcing install.sh as a library."""
    script = f'FLIGHTRISK_INSTALL_LIB_ONLY=1\nsource "{INSTALL}"\nset +e\n{snippet}\n'
    return subprocess.run(["/bin/bash", "-c", script], env=env or {**os.environ}, capture_output=True, text=True,
                          timeout=timeout, check=False)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shell_scripts_pass_shellcheck():
    scripts = sorted(MACOS.glob("*.sh")) + [ROOT / "installer" / "update-locks.sh"]
    result = subprocess.run(["shellcheck", "-x", *map(str, scripts)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout


def test_bash_syntax_of_every_script():
    for script in sorted(MACOS.glob("*.sh")):
        result = subprocess.run(["/bin/bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{script.name}: {result.stderr}"


# ---------------------------------------------------------------------------
# install.sh functions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["v0.1.0", "v0.2.0", "v0.10.0", "v0.9.9"], "v0.10.0"),
        (["v1.0.0", "v1.0.0-rc1", "v2.0.0-beta", "hackathon/kickoff"], "v1.0.0"),
        (["v2.0.0", "v10.0.0", "v9.99.99"], "v10.0.0"),
        (["hackathon/kickoff", "release-1", "1.2.3"], ""),
        ([], ""),
    ],
)
def test_pick_latest_tag_takes_the_newest_plain_release(tags, expected):
    lines = "".join(f"{'ab' * 20}\trefs/tags/{tag}\n" for tag in tags)
    result = lib('printf %s "$TAG_LINES" | pick_latest_tag', env={**os.environ, "TAG_LINES": lines})
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(("ref", "ok"), [("v1.2.3", True), ("main", True), ("v1.2", False), ("1.2.3", False),
                                         ("v1.2.3-rc1", False), ("Main", False), ("v1.2.3; rm -rf /", False)])
def test_valid_ref(ref, ok):
    result = lib('valid_ref "$REF_TO_CHECK" && echo yes || echo no', env={**os.environ, "REF_TO_CHECK": ref})
    assert result.stdout.strip() == ("yes" if ok else "no")


def test_status_file_is_valid_json_for_any_message(tmp_path):
    status = tmp_path / "status.json"
    message = 'quote " backslash \\ tab \t newline \n control \x01 done'
    env = {**os.environ, "FLIGHTRISK_STATUS_FILE": str(status), "FLIGHTRISK_HOME": str(tmp_path)}
    result = lib('STEP=packages; REF=v1.2.3; write_status error "$MESSAGE"', env={**env, "MESSAGE": message})
    assert result.returncode == 0, result.stderr
    data = json.loads(status.read_text())
    assert set(data) == {"state", "step", "message", "ref", "pid", "at"}
    assert data["state"] == "error" and data["step"] == "packages" and data["ref"] == "v1.2.3"
    assert data["message"] == message.replace("\x01", "")
    assert isinstance(data["pid"], int)
    assert data["at"].endswith("Z")
    assert not list(tmp_path.glob("status.json.*")), "the temporary file is renamed, not left behind"


def test_status_without_message_or_ref_uses_null(tmp_path):
    status = tmp_path / "status.json"
    env = {**os.environ, "FLIGHTRISK_STATUS_FILE": str(status), "FLIGHTRISK_HOME": str(tmp_path)}
    lib('write_status running ""', env=env)
    data = json.loads(status.read_text())
    assert data["message"] is None and data["ref"] is None and data["step"] == "start"


def _fake_python(path: Path, version: str) -> Path:
    path.write_text(f'#!/bin/sh\necho "{version}"\n')
    path.chmod(0o755)
    return path


def test_find_python_skips_wrong_and_broken_versions(tmp_path):
    old = _fake_python(tmp_path / "python3.12", "3.12")
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    good = _fake_python(tmp_path / "python3.13", "3.13")
    env = {**os.environ, "FLIGHTRISK_PYTHON_CANDIDATES": f"{old}:{tmp_path / 'missing'}:{broken}:{good}"}
    env.pop("FLIGHTRISK_PYTHON", None)
    result = lib('find_python && echo "$PYTHON_BIN"', env=env)
    assert result.stdout.strip() == str(good)


def test_find_python_fails_without_a_3_13(tmp_path):
    old = _fake_python(tmp_path / "python3.12", "3.12")
    env = {**os.environ, "FLIGHTRISK_PYTHON_CANDIDATES": str(old)}
    env.pop("FLIGHTRISK_PYTHON", None)
    assert lib("find_python && echo found || echo none", env=env).stdout.strip() == "none"


def test_flightrisk_python_must_be_3_13(tmp_path):
    old = _fake_python(tmp_path / "python3.12", "3.12")
    env = {**os.environ, "FLIGHTRISK_PYTHON": str(old), "FLIGHTRISK_HOME": str(tmp_path)}
    result = lib("find_python; echo unreachable", env=env)
    assert result.returncode == 1
    assert "is not a working Python 3.13" in result.stderr
    assert "unreachable" not in result.stdout


def _recording_brew(shims: Path, creates: dict[str, str]) -> Path:
    """A fake brew that logs its arguments and, for `brew install X`, writes the script creates[X]."""
    lines = ['#!/bin/bash', 'echo "brew $*" >> "$BREW_LOG"', 'if [ "$1" = shellenv ]; then exit 0; fi']
    for formula, body in creates.items():
        lines.append(f'if [ "$1 $2" = "install {formula}" ]; then cat > {body}; fi')
    brew = shims / "brew"
    brew.write_text("\n".join(lines) + "\nexit 0\n")
    brew.chmod(0o755)
    return brew


def test_missing_git_is_installed_with_homebrew(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "brew.log"
    git_body = f"'{shims}/git' <<'EOF'\n#!/bin/sh\necho 'git version 9.9'\nEOF\nchmod +x '{shims}/git'"
    _recording_brew(shims, {"git": git_body})
    env = {**os.environ, "PATH": f"{shims}:/usr/bin:/bin", "BREW_LOG": str(log), "FLIGHTRISK_HOME": str(tmp_path)}
    # The first look finds no git; after brew install the fake one in shims/ is found.
    snippet = f'''
calls=0
have() {{ if [ "$1" = git ]; then [ -x "{shims}/git" ]; else command -v "$1" >/dev/null 2>&1; fi; }}
git() {{ "{shims}/git" "$@"; }}
ensure_git && echo ensured
'''
    result = lib(snippet, env=env)
    assert "ensured" in result.stdout, result.stdout + result.stderr
    assert log.read_text().splitlines() == ["brew install git"]


def test_missing_python_is_installed_with_homebrew(tmp_path):
    shims = tmp_path / "shims"
    shims.mkdir()
    log = tmp_path / "brew.log"
    target = tmp_path / "python3.13"
    py_body = f"'{target}' <<'EOF'\n#!/bin/sh\necho 3.13\nEOF\nchmod +x '{target}'"
    _recording_brew(shims, {"python@3.13": py_body})
    env = {**os.environ, "PATH": f"{shims}:/usr/bin:/bin", "BREW_LOG": str(log), "FLIGHTRISK_HOME": str(tmp_path),
           "FLIGHTRISK_PYTHON_CANDIDATES": str(target)}
    env.pop("FLIGHTRISK_PYTHON", None)
    result = lib('ensure_python && echo "$PYTHON_BIN"', env=env)
    assert result.stdout.strip().splitlines()[-1] == str(target), result.stdout + result.stderr
    assert log.read_text().splitlines() == ["brew install python@3.13"]


def test_homebrew_is_installed_only_when_something_is_missing(tmp_path):
    env = {**os.environ, "PATH": "/usr/bin:/bin", "FLIGHTRISK_HOME": str(tmp_path), "FLIGHTRISK_BREW_CANDIDATES": ""}
    snippet = '''
install_homebrew() { echo "would install homebrew"; return 1; }
find_brew && echo "found brew" || echo "no brew"
ensure_brew || echo "ensure failed"
'''
    result = lib(snippet, env=env)
    assert result.stdout.splitlines() == ["no brew", "would install homebrew", "ensure failed"]


def test_install_lock_refuses_a_live_holder_and_takes_over_a_stale_one(tmp_path):
    env = {**os.environ, "FLIGHTRISK_HOME": str(tmp_path)}
    holder = subprocess.Popen(["sleep", "30"])
    try:
        lock = tmp_path / "install.lock.d"
        lock.mkdir()
        (lock / "pid").write_text(f"{holder.pid}\n")
        assert lib("acquire_lock && echo got || echo busy", env=env).stdout.strip() == "busy"
    finally:
        holder.kill()
        holder.wait()
    result = lib('acquire_lock && echo got && cat "$LOCK_DIR/pid" && echo "$$" && release_lock && ls "$LOCK_DIR"', env=env)
    lines = result.stdout.split()
    assert lines[0] == "got" and lines[1] == lines[2]
    assert not (tmp_path / "install.lock.d").exists()


def test_running_pids_finds_programs_started_from_the_venv(tmp_path):
    venv_bin = tmp_path / "Application Support" / "FlightRisk" / "app" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake = venv_bin / "python"
    fake.symlink_to(shutil.which("sleep"))
    other = subprocess.Popen(["sleep", "30"])
    proc = subprocess.Popen([str(fake), "30"])
    try:
        env = {**os.environ, "FLIGHTRISK_HOME": str(tmp_path / "Application Support" / "FlightRisk")}
        pids = lib("running_pids", env=env).stdout.split()
        assert str(proc.pid) in pids
        assert str(other.pid) not in pids
    finally:
        for p in (proc, other):
            p.kill()
            p.wait()


# ---------------------------------------------------------------------------
# install.sh end to end (no packages)
# ---------------------------------------------------------------------------


def test_install_creates_checkout_marker_venv_app_and_status(installed: Box):
    box = installed
    assert box.head() == box.tag_commit("v0.2.0"), "the newest plain release, not the release candidate"
    marker = (box.app_dir / ".git" / "flightrisk-installer").read_text()
    assert "ref=v0.2.0" in marker and "installer=installer/macos/install.sh" in marker
    version = subprocess.run([str(box.venv / "bin" / "python"), "-c", "import sys; print(sys.version_info[:2])"],
                             capture_output=True, text=True, check=True).stdout.strip()
    assert version == "(3, 13)"
    status = box.status_json()
    assert status["state"] == "done" and status["step"] == "done" and status["ref"] == "v0.2.0"
    assert not (box.flightrisk_home / "install.lock.d").exists()
    assert box.brew_calls() == [], "git and Python were found, so Homebrew is never called"
    assert box.bundle.is_dir()


def test_app_bundle_structure(installed: Box):
    contents = installed.bundle / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == BUNDLE_ID
    assert info["CFBundleExecutable"] == "FlightRisk"
    assert info["CFBundleIconFile"] == "FlightRisk"
    assert info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleShortVersionString"] == "0.2.0"
    assert info["LSUIElement"] is True
    executable = contents / "MacOS" / "FlightRisk"
    assert os.access(executable, os.X_OK)
    assert executable.read_bytes() == (MACOS / "app-main.sh").read_bytes()
    assert (contents / "Resources" / "FlightRisk.icns").read_bytes()[:4] == b"icns"
    assert (contents / "Resources" / "install.sh").read_bytes() == INSTALL.read_bytes()
    assert (contents / "Resources" / "install-ref").read_text().strip() == "v0.2.0"
    assert (contents / "PkgInfo").read_text() == "APPL????"


def test_the_edge_token_never_reaches_the_installer_output(box: Box):
    token = "edge-token-that-must-never-be-printed"
    (box.home / ".flightrisk-fleet.env").write_text(f"export FLIGHTRISK_EDGE_TOKEN={token}\n")
    result = box.install(FLIGHTRISK_EDGE_TOKEN=token)
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr
    written = [box.status, box.app_dir / ".git" / "flightrisk-installer", *box.bundle.rglob("*")]
    for path in written:
        if path.is_file():
            assert token.encode() not in path.read_bytes(), path


def test_rerun_is_idempotent(installed: Box):
    head = installed.head()
    result = installed.install()
    assert result.returncode == 0, result.stderr
    assert "already at v0.2.0" in result.stdout
    assert "virtual environment found" in result.stdout
    assert installed.head() == head


def test_never_moves_backwards_unless_allowed(installed: Box):
    result = installed.install(FLIGHTRISK_REF="v0.1.0")
    assert result.returncode == 0
    assert "the installed version is newer" in result.stdout
    assert installed.head() == installed.tag_commit("v0.2.0")
    assert "ref=v0.2.0" in (installed.app_dir / ".git" / "flightrisk-installer").read_text()
    result = installed.install(FLIGHTRISK_REF="v0.1.0", FLIGHTRISK_ALLOW_DOWNGRADE="1")
    assert result.returncode == 0
    assert installed.head() == installed.tag_commit("v0.1.0")


def test_bad_ref_is_refused(box: Box):
    result = box.install(FLIGHTRISK_REF="v1; touch /tmp/x")
    assert result.returncode == 1
    assert "must be a release tag" in result.stderr
    assert box.status_json()["state"] == "error"
    assert not box.app_dir.exists()


def test_unknown_release_removes_the_half_made_folder(box: Box):
    result = box.install(FLIGHTRISK_REF="v9.9.9")
    assert result.returncode == 1
    assert "v9.9.9" in result.stderr
    assert not box.app_dir.exists(), "a folder this run created and could not finish is removed"


def test_local_changes_block_an_update(installed: Box):
    (installed.app_dir / "requirements.txt").write_text("changed\n")
    _git("tag", "v0.3.0", cwd=installed.repo)
    result = installed.install()
    assert result.returncode == 1
    assert "has local changes" in result.stderr
    assert installed.status_json()["state"] == "error"
    assert installed.head() == installed.tag_commit("v0.2.0")


def test_folder_not_made_by_the_installer_is_never_changed(box: Box):
    box.flightrisk_home.mkdir(parents=True)
    _git("clone", "-q", str(box.repo), str(box.app_dir), cwd=box.root)
    before = _git("rev-parse", "HEAD", cwd=box.app_dir)
    result = box.install()
    assert result.returncode == 1
    assert "was not made by this installer" in result.stderr
    assert _git("rev-parse", "HEAD", cwd=box.app_dir) == before
    assert not box.venv.exists()


def test_a_non_empty_unrelated_folder_is_refused(box: Box):
    box.app_dir.mkdir(parents=True)
    (box.app_dir / "notes.txt").write_text("mine")
    result = box.install()
    assert result.returncode == 1
    assert "is not a FlightRisk install made by this installer" in result.stderr
    assert (box.app_dir / "notes.txt").read_text() == "mine"


def test_an_unrelated_app_of_the_same_name_is_left_alone(box: Box):
    foreign = box.applications / "FlightRisk.app" / "Contents"
    foreign.mkdir(parents=True)
    (foreign / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
    result = box.install()
    assert result.returncode == 0
    assert "is not FlightRisk's launcher app" in result.stderr
    assert plistlib.loads((foreign / "Info.plist").read_bytes())["CFBundleIdentifier"] == "com.example.other"


def test_installs_main_when_no_release_exists(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    make_fixture_repo(repo)
    for tag in ("v0.1.0", "v0.2.0", "v1.0.0-rc1"):
        _git("tag", "-d", tag, cwd=repo)
    box = Box(tmp_path / "box", repo)
    result = box.install()
    assert result.returncode == 0, result.stderr
    assert "no release has been published yet" in result.stdout
    assert box.head() == _git("rev-parse", "main", cwd=repo)
    assert "ref=main" in (box.app_dir / ".git" / "flightrisk-installer").read_text()


def test_failed_package_install_puts_the_previous_release_back(installed: Box):
    _git("tag", "v0.3.0", cwd=installed.repo)
    # Packages are installed for real here, from an index that cannot be reached, so pip fails fast.
    result = installed.install(FLIGHTRISK_INSTALL_SKIP_DEPS="", PIP_INDEX_URL="http://127.0.0.1:9/simple",
                               PIP_RETRIES="0", PIP_TIMEOUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                               PIP_NO_INPUT="1", timeout=300)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "put" in result.stdout and "back on the version it had before" in result.stdout
    assert installed.head() == installed.tag_commit("v0.2.0")
    assert installed.status_json()["state"] == "error"


# ---------------------------------------------------------------------------
# update.sh
# ---------------------------------------------------------------------------


def test_update_check_and_update(installed: Box):
    update = installed.app_dir / "installer" / "macos" / "update.sh"
    check = installed.run(update, "--check")
    assert check.returncode == 0, check.stderr
    data = json.loads(check.stdout)
    assert data == {"installDir": str(installed.app_dir), "installed": "v0.2.0", "latest": "v0.2.0",
                    "updateAvailable": False}
    _git("commit", "-q", "--allow-empty", "-m", "third", cwd=installed.repo)
    _git("tag", "v0.3.0", cwd=installed.repo)
    data = json.loads(installed.run(update, "--check").stdout)
    assert data["latest"] == "v0.3.0" and data["updateAvailable"] is True
    result = installed.run(update)
    assert result.returncode == 0, result.stdout + result.stderr
    assert installed.head() == installed.tag_commit("v0.3.0")
    assert "ref=v0.3.0" in (installed.app_dir / ".git" / "flightrisk-installer").read_text()


def test_update_refuses_an_install_it_did_not_make(box: Box):
    box.flightrisk_home.mkdir(parents=True)
    _git("clone", "-q", str(box.repo), str(box.app_dir), cwd=box.root)
    result = box.run(MACOS / "update.sh")
    assert result.returncode == 2
    assert "not a FlightRisk install made by the installer" in result.stderr


# ---------------------------------------------------------------------------
# uninstall.sh
# ---------------------------------------------------------------------------


def _uninstall(box: Box, *args: str) -> subprocess.CompletedProcess:
    return box.run(box.app_dir / "installer" / "macos" / "uninstall.sh", *args)


def _user_data(box: Box) -> dict[Path, str]:
    files = {
        box.home / ".flightrisk" / "launcher.json": '{"presets": {}}',
        box.home / ".flightrisk-fleet.env": "export FLIGHTRISK_EDGE_TOKEN=not-a-real-token\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return files


def test_uninstall_check_changes_nothing(installed: Box):
    (installed.app_dir / "logs").mkdir()
    result = installed.run(MACOS / "uninstall.sh", "--check")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["exists"] and data["flightriskCheckout"] and data["installerMade"] and data["appIsOurs"]
    assert data["isLink"] is False and data["running"] == [] and data["unsaved"] == []
    assert data["keep"] == ["logs"]
    assert installed.app_dir.exists() and installed.bundle.exists()


def test_uninstall_removes_the_install_and_keeps_user_data(installed: Box):
    user_files = _user_data(installed)
    (installed.app_dir / "logs" / "launcher").mkdir(parents=True)
    (installed.app_dir / "logs" / "launcher" / "ground.log").write_text("log line")
    (installed.app_dir / "captures").mkdir()
    (installed.app_dir / "captures" / "frame.jpg").write_bytes(b"\xff\xd8")
    log_dir = installed.home / "Library" / "Logs" / "FlightRisk"
    log_dir.mkdir(parents=True)
    result = _uninstall(installed, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["removed"] is True and data["appRemoved"] is True and data["alreadyGone"] is False
    assert not installed.app_dir.exists()
    assert not installed.bundle.exists()
    assert not installed.flightrisk_home.exists()
    assert not log_dir.exists()
    kept = Path(data["keptData"])
    assert kept.parent == installed.home / "Documents"
    assert (kept / "logs" / "launcher" / "ground.log").read_text() == "log line"
    assert (kept / "captures" / "frame.jpg").exists()
    for path, text in user_files.items():
        assert path.read_text() == text, f"{path} must be kept unchanged"
    assert "not-a-real-token" not in result.stdout + result.stderr


def test_uninstall_without_keeping_data(installed: Box):
    (installed.app_dir / "logs").mkdir()
    result = _uninstall(installed, "--yes", "--no-keep-data")
    assert result.returncode == 0
    assert json.loads(result.stdout)["keptData"] is None
    assert not (installed.home / "Documents").exists()


def test_uninstall_refuses_a_checkout_without_marker(installed: Box):
    (installed.app_dir / ".git" / "flightrisk-installer").unlink()
    result = installed.run(MACOS / "uninstall.sh", "--yes", "--force")
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "not-made-by-installer"
    assert installed.app_dir.exists() and installed.bundle.exists()


def test_uninstall_refuses_a_link(box: Box, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    box.flightrisk_home.mkdir(parents=True)
    box.app_dir.symlink_to(real)
    result = box.run(MACOS / "uninstall.sh", "--yes", "--force")
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "is-a-link"
    assert real.exists()


def test_uninstall_refuses_unsaved_work_unless_forced(installed: Box):
    (installed.app_dir / "README-local.txt").write_text("x")
    _git("add", "README-local.txt", cwd=installed.app_dir)
    _git("commit", "-qm", "local work", cwd=installed.app_dir)
    result = _uninstall(installed, "--yes")
    assert result.returncode == 4
    assert json.loads(result.stdout)["reason"] == "unsaved-work"
    assert "commits that are not pushed: 1" in result.stderr
    assert installed.app_dir.exists()
    assert _uninstall(installed, "--yes", "--force").returncode == 0
    assert not installed.app_dir.exists()


def test_uninstall_refuses_while_flightrisk_runs(installed: Box):
    proc = subprocess.Popen([str(installed.venv / "bin" / "python"), "-c", "import time; time.sleep(60)"])
    try:
        result = _uninstall(installed, "--yes", "--force")
        assert result.returncode == 3
        assert json.loads(result.stdout)["reason"] == "running"
        assert str(proc.pid) in result.stderr
        assert installed.app_dir.exists()
        assert proc.poll() is None, "the uninstaller never stops a FlightRisk process"
    finally:
        proc.kill()
        proc.wait()


def test_install_refuses_an_update_while_flightrisk_runs(installed: Box):
    _git("tag", "v0.3.0", cwd=installed.repo)
    proc = subprocess.Popen([str(installed.venv / "bin" / "python"), "-c", "import time; time.sleep(60)"])
    try:
        result = installed.install()
        assert result.returncode == 1
        assert "FlightRisk is running" in result.stderr
        assert installed.head() == installed.tag_commit("v0.2.0")
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


def test_uninstall_after_the_folder_was_deleted_by_hand(installed: Box):
    uninstall_copy = installed.root / "uninstall.sh"
    shutil.copy2(installed.app_dir / "installer" / "macos" / "uninstall.sh", uninstall_copy)
    shutil.rmtree(installed.app_dir)
    result = installed.run(uninstall_copy, "--yes")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["alreadyGone"] is True and data["appRemoved"] is True
    assert not installed.bundle.exists()


# ---------------------------------------------------------------------------
# FlightRisk.app
# ---------------------------------------------------------------------------


def _app_env(box: Box, port: int) -> dict[str, str]:
    return {
        "HOME": str(box.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
        "FLIGHTRISK_OPEN_IN": "none", "FLIGHTRISK_APP_NONINTERACTIVE": "1", "FLIGHTRISK_LAUNCHER_PORT": str(port),
    }


def _launcher_api(url: str):
    parts = urllib.parse.urlsplit(url)
    key = urllib.parse.parse_qs(parts.query)["key"][0]
    base = f"http://{parts.netloc}"

    def call(path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data,
                                         headers={"X-Launcher-Session": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)

    return call


def _wait_gone(pid: int, timeout: float = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.2)
    return False


def _venv_processes(box: Box) -> list[int]:
    out = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True, text=True, check=True).stdout
    prefix = f"{box.venv}/"
    return [int(line.split(None, 1)[0]) for line in out.splitlines()
            if line.strip() and line.split(None, 1)[1].startswith(prefix)]


@pytest.fixture
def runnable(installed: Box):
    """The fixture install, with this test run's packages importable from its venv."""
    site_packages = next((installed.venv / "lib").glob("python3.13/site-packages"))
    (site_packages / "test-run-packages.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    yield installed
    for pid in _venv_processes(installed):
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def test_app_offers_the_install_when_flightrisk_is_missing(box: Box, tmp_path):
    built = subprocess.run(["/bin/bash", str(MACOS / "make-app.sh"), "build", "--out", str(tmp_path / "out")],
                           capture_output=True, text=True, check=True).stdout.strip()
    result = subprocess.run([f"{built}/Contents/MacOS/FlightRisk"], env=_app_env(box, free_port()),
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 3
    app_log = (box.home / "Library" / "Logs" / "FlightRisk" / "app.log").read_text()
    assert "not installed; the install was not started" in app_log


def test_app_starts_the_launcher_once_and_reopens_its_window(runnable: Box):
    box = runnable
    port = free_port()
    executable = box.bundle / "Contents" / "MacOS" / "FlightRisk"
    first = subprocess.run([str(executable)], env=_app_env(box, port), capture_output=True, text=True, timeout=90,
                           check=False)
    assert first.returncode == 0, first.stderr
    run_dir = box.flightrisk_home / "run"
    url = (run_dir / "launcher.url").read_text().strip()
    pid = int((run_dir / "launcher.pid").read_text())
    assert url.startswith(f"http://127.0.0.1:{port}/?key=")
    for private in (run_dir / "launcher.url", box.home / "Library" / "Logs" / "FlightRisk" / "launcher.log"):
        assert private.stat().st_mode & 0o077 == 0, f"{private} holds the window key and must be private"
    with urllib.request.urlopen(url, timeout=10) as page:
        assert page.status == 200
    call = _launcher_api(url)
    presets = {p["name"] for p in call("/api/bootstrap")["presets"]}
    assert "Rehearsal with fake drones" in presets

    second = subprocess.run([str(executable)], env=_app_env(box, port), capture_output=True, text=True, timeout=30,
                            check=False)
    assert second.returncode == 0
    assert (run_dir / "launcher.url").read_text().strip() == url
    assert int((run_dir / "launcher.pid").read_text()) == pid
    assert _venv_processes(box) == [pid], "a second open reuses the running launcher"

    assert call("/api/quit", {"action": "leave"}) == {"quitting": "leave"}
    assert _wait_gone(pid)


def test_app_reports_a_launcher_that_cannot_start(runnable: Box):
    box = runnable
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        result = subprocess.run([str(box.bundle / "Contents" / "MacOS" / "FlightRisk")], env=_app_env(box, port),
                                capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 1
    app_log = (box.home / "Library" / "Logs" / "FlightRisk" / "app.log").read_text()
    assert "The FlightRisk launcher did not start" in app_log
    assert not (box.flightrisk_home / "run" / "launcher.pid").exists()


def _weights() -> Path | None:
    for folder in (ROOT, Path(sys.prefix).resolve().parent):
        if (folder / "yolo11n.pt").is_file():
            return folder / "yolo11n.pt"
    return None


@pytest.mark.slow
@pytest.mark.skipif(_weights() is None, reason="yolo11n.pt is not on disk")
def test_app_launcher_runs_the_fake_drone_rehearsal(runnable: Box):
    """The whole path: app, launcher, then ground and two fake edges on free ports, then Quit with stop."""
    box = runnable
    (box.app_dir / "yolo11n.pt").symlink_to(_weights())
    result = subprocess.run([str(box.bundle / "Contents" / "MacOS" / "FlightRisk")],
                            env=_app_env(box, free_port()), capture_output=True, text=True, timeout=90, check=False)
    assert result.returncode == 0, result.stderr
    call = _launcher_api((box.flightrisk_home / "run" / "launcher.url").read_text().strip())
    launcher_pid = int((box.flightrisk_home / "run" / "launcher.pid").read_text())
    settings = next(p["settings"] for p in call("/api/bootstrap")["presets"] if p["name"] == "Rehearsal with fake drones")
    settings["dashboard_port"], settings["edge_port"] = free_port(), free_port()
    started = call("/api/start", {"settings": settings})
    assert started["started"] is True, started
    deadline = time.time() + 180
    states: dict[str, str] = {}
    while time.time() < deadline:
        states = {p["name"]: p["status"] for p in call("/api/state")["processes"]}
        if len(states) == 3 and set(states.values()) == {"running"}:
            break
        time.sleep(2)
    assert states == {"ground": "running", "edge-drone-1": "running", "edge-drone-2": "running"}
    call("/api/quit", {"action": "stop"})
    assert _wait_gone(launcher_pid, timeout=90)
    deadline = time.time() + 30
    while _venv_processes(box) and time.time() < deadline:
        time.sleep(0.5)
    assert _venv_processes(box) == []


# ---------------------------------------------------------------------------
# make-app.sh and the .dmg
# ---------------------------------------------------------------------------


def test_make_app_replaces_its_own_bundle_and_refuses_a_foreign_one(tmp_path):
    make_app = MACOS / "make-app.sh"
    first = subprocess.run(["/bin/bash", str(make_app), "build", "--out", str(tmp_path)], capture_output=True,
                           text=True, check=True).stdout.strip()
    stale = Path(first) / "Contents" / "Resources" / "stale.txt"
    stale.write_text("old")
    subprocess.run(["/bin/bash", str(make_app), "build", "--out", str(tmp_path), "--ref", "v2.3.4"], check=True,
                   capture_output=True)
    assert not stale.exists()
    info = plistlib.loads((Path(first) / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleShortVersionString"] == "2.3.4"
    other = tmp_path / "other"
    (other / "FlightRisk.app" / "Contents").mkdir(parents=True)
    result = subprocess.run(["/bin/bash", str(make_app), "build", "--out", str(other)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 1
    assert not (other / "FlightRisk.app" / "Contents" / "MacOS").exists()


@pytest.mark.skipif(shutil.which("hdiutil") is None, reason="hdiutil not available")
def test_build_dmg_holds_the_app_and_an_applications_link(tmp_path):
    result = subprocess.run(["/bin/bash", str(MACOS / "build-dmg.sh"), "--out", str(tmp_path), "--ref", "v1.2.3"],
                            capture_output=True, text=True, timeout=180, check=False)
    assert result.returncode == 0, result.stderr
    dmg = Path(result.stdout.strip())
    assert dmg == tmp_path / "FlightRisk-1.2.3-arm64.dmg" and dmg.is_file()
    mount = tmp_path / "mnt"
    mount.mkdir()
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount), str(dmg)],
                   check=True, capture_output=True, timeout=120)
    try:
        assert sorted(p.name for p in mount.iterdir() if not p.name.startswith(".")) == [
            "Applications", "FlightRisk.app", "Read me first.txt"]
        assert os.readlink(mount / "Applications") == "/Applications"
        app = mount / "FlightRisk.app"
        assert plistlib.loads((app / "Contents" / "Info.plist").read_bytes())["CFBundleIdentifier"] == BUNDLE_ID
        assert os.access(app / "Contents" / "MacOS" / "FlightRisk", os.X_OK)
        assert (app / "Contents" / "Resources" / "install-ref").read_text().strip() == "v1.2.3"
        verify = subprocess.run(["codesign", "--verify", "--strict", str(app)], capture_output=True, text=True,
                                check=False)
        assert verify.returncode == 0, verify.stderr
        assert "Open Anyway" in (mount / "Read me first.txt").read_text()
    finally:
        subprocess.run(["hdiutil", "detach", "-quiet", str(mount)], check=False, capture_output=True, timeout=60)
