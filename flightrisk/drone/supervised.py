"""Child processes that die with the process that started them.

The MAVLink link runs ``mavsdk_server`` as a child process. MAVSDK-Python
starts it with a plain ``Popen`` and kills it only from an ``atexit`` hook,
so after a SIGKILL, the out-of-memory killer or a native crash of the edge
the server lives on: it keeps sending heartbeats (so PX4's data-link-loss
failsafe never fires) and keeps the serial port open (so a restarted edge
cannot connect).

``spawn_tied_to_parent`` starts the command under a tiny ``/bin/sh``
supervisor that polls the parent once a second and kills the command when
the parent is gone, on Linux and macOS alike, so the command is stopped
within about a second of any death of the edge, however it died. (Linux's
``PR_SET_PDEATHSIG`` would be instant but needs ``preexec_fn``, which is not
safe in a process with threads; the poll needs neither.) Windows is not
covered (the edge's companion computers run Linux).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = ["spawn_tied_to_parent"]

# $1 is the parent pid, the rest is the command. The command runs in the
# background; the loop ends when the parent or the command is gone, then the
# command is killed. TERM (from ``Popen.terminate``) kills it at once.
_SUPERVISOR = r'''
parent=$1; shift
"$@" &
child=$!
trap 'kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT HUP
while kill -0 "$parent" 2>/dev/null && kill -0 "$child" 2>/dev/null; do sleep 1; done
kill "$child" 2>/dev/null
wait "$child" 2>/dev/null
'''


def spawn_tied_to_parent(cmd: Sequence[str], *, log_name: str = "supervised") -> subprocess.Popen:
    """Start *cmd* so that it is killed within about a second when this process dies.

    Returns the supervisor's ``Popen``; ``terminate()`` on it stops the
    command too. The command's output is logged at debug level under
    *log_name*.
    """
    if sys.platform.startswith("win"):
        raise OSError("spawn_tied_to_parent needs a POSIX shell")
    proc = subprocess.Popen(
        ["/bin/sh", "-c", _SUPERVISOR, "supervisor", str(os.getpid()), *cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    out = logging.getLogger(log_name)

    def _pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            out.debug(raw.decode("utf-8", "replace").rstrip())

    threading.Thread(target=_pump, daemon=True, name=f"{log_name}-log").start()
    return proc
