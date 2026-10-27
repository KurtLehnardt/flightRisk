"""Checks that docs/built-this-week.md agrees with git.

Requirement covered: R-060. Every path tagged "(new)" must be absent from the
kickoff commit and present on disk, and every path tagged "(changed)" must
exist at the kickoff commit.
"""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "built-this-week.md"
KICKOFF = "798f21a"

TAGGED_PATH = re.compile(r"^- `([^`]+)` \((new|changed)\)\s*$", re.M)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture(scope="module")
def kickoff_files() -> set[str]:
    try:
        return set(_git("ls-tree", "-r", "--name-only", KICKOFF).splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(f"commit {KICKOFF} is not available in this checkout")


def _paths(tag: str) -> list[str]:
    return [p for p, t in TAGGED_PATH.findall(DOC.read_text()) if t == tag]


def test_mentions_kickoff_sha_and_sections():
    text = DOC.read_text()
    assert KICKOFF in text
    assert "hackathon/kickoff" in text
    assert re.search(r"^## Before kickoff", text, re.M)
    assert re.search(r"^## Built this week", text, re.M)
    assert f"git log --oneline {KICKOFF}..HEAD" in text
    assert "676" in text
    assert re.search(r"\d+ passed", text)
    assert _paths("new") and _paths("changed")


def test_new_paths_absent_at_kickoff_and_present_now(kickoff_files):
    new = _paths("new")
    assert new
    for path in new:
        assert path not in kickoff_files, f"{path} existed at kickoff"
        assert (ROOT / path).exists(), f"{path} is missing on disk"


def test_changed_paths_exist_at_kickoff(kickoff_files):
    changed = _paths("changed")
    assert changed
    for path in changed:
        assert path in kickoff_files, f"{path} did not exist at kickoff"
        assert (ROOT / path).exists(), f"{path} is missing on disk"
