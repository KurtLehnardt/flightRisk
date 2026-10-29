"""The edge lands on Windows Ctrl+Break (SIGBREAK) exactly as on Ctrl+C, so the launcher can stop it safely.

SIGBREAK exists only on Windows, so these tests use a stand-in for the signal module.
"""

from __future__ import annotations

import signal
import types

import pytest

from flightrisk.edge_device import _install_sigint_land, _restore_sigbreak


class Log:
    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class Flight:
    def __init__(self):
        self.lands = []

    def land_now(self, reason):
        self.lands.append(reason)


def _signals(with_break: bool):
    installed = {}

    def install(sig, handler):
        previous = installed.get(sig, "default")
        installed[sig] = handler
        return previous

    module = types.SimpleNamespace(SIGINT=2, SIG_DFL="default", signal=install)
    if with_break:
        module.SIGBREAK = 21
    return module, installed


def test_sigbreak_gets_the_same_landing_handler_as_sigint():
    module, installed = _signals(with_break=True)
    flight = Flight()
    previous = _install_sigint_land(flight, Log(), signals=module)
    assert previous == "default"
    assert installed[21] is installed[2]
    with pytest.raises(KeyboardInterrupt):
        installed[21](21, None)
    assert flight.lands == ["sigint"]
    _restore_sigbreak(module)
    assert installed[21] == "default"


def test_without_sigbreak_only_sigint_is_installed():
    module, installed = _signals(with_break=False)
    _install_sigint_land(Flight(), Log(), signals=module)
    assert set(installed) == {2}
    _restore_sigbreak(module)
    assert set(installed) == {2}


def test_posix_signal_module_has_no_sigbreak_so_behaviour_is_unchanged():
    if hasattr(signal, "SIGBREAK"):
        pytest.skip("this platform has SIGBREAK (Windows)")
    flight = Flight()
    previous = _install_sigint_land(flight, Log())
    try:
        handler = signal.getsignal(signal.SIGINT)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)
        assert flight.lands == ["sigint"]
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)
