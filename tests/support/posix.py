"""The POSIX launcher scripts (``scripts/*.sh``) run under bash, which a Windows host does not have."""

from __future__ import annotations

import sys

import pytest


def posix_bash() -> str:
    """The bash to run a ``.sh`` script with, or skip the test on Windows.

    The Windows laptop uses the PowerShell launchers; ``bash`` on a Windows runner is the WSL stub with no
    distribution installed, which cannot run these scripts.
    """
    if sys.platform == "win32":
        pytest.skip("the .sh launchers are for macOS and Linux; Windows uses the .ps1 launchers")
    return "bash"
