"""A local git repository to install FlightRisk from in the installer tests (never GitHub)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: The files the fixture repository needs: the package, the installer and the requirement files.
FIXTURE_PATHS = ["flightrisk", "installer", "scripts/launcher.command", "scripts/launcher.bat",
                 "requirements.txt", "requirements-edge.txt"]


def git(*args: str, cwd: Path) -> str:
    """Run git in *cwd* with a fixed test identity and return its output."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def make_fixture_repo(path: Path) -> Path:
    """A repository with this checkout's installer and package, tagged v0.1.0, v0.2.0 and v1.0.0-rc1.

    The files are copied from the working tree (committed or not), so the tests exercise the
    scripts as they are on disk.
    """
    files = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *FIXTURE_PATHS],
                           cwd=ROOT, check=True, capture_output=True, text=True).stdout.split("\n")
    for name in filter(None, files):
        source = ROOT / name
        if source.is_file():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    git("init", "-q", "-b", "main", cwd=path)
    git("add", "-A", cwd=path)
    git("commit", "-qm", "first", cwd=path)
    git("tag", "v0.1.0", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "second", cwd=path)
    git("tag", "v0.2.0", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "candidate", cwd=path)
    git("tag", "v1.0.0-rc1", cwd=path)
    return path
