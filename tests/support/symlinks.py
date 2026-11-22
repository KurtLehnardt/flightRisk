"""Creating symlinks in tests, which Windows allows only with a privilege most accounts lack."""

from __future__ import annotations

from pathlib import Path

import pytest

_PRIVILEGE_NOT_HELD = 1314  # ERROR_PRIVILEGE_NOT_HELD, from creating a symlink without Developer Mode or elevation


def symlink_or_skip(link: Path, target: Path | str) -> None:
    """``link.symlink_to(target)``, or skip the test when Windows refuses the symlink for lack of the privilege."""
    try:
        link.symlink_to(target)
    except OSError as err:
        if getattr(err, "winerror", None) == _PRIVILEGE_NOT_HELD:
            pytest.skip("creating a symlink needs Developer Mode or an elevated Windows shell (WinError 1314)")
        raise
