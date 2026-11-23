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
covered by the supervisor; see below.

On Windows there is no ``/bin/sh``. The command is started directly and
assigned to a Windows Job Object created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``
(through ``ctypes``). The job handle lives as long as this process, and
Windows closes it when the process ends however it ends, which terminates
every process in the job, so a crashed edge cannot leave ``mavsdk_server.exe``
running. If the process cannot be assigned to a job (for example it already
belongs to a job that forbids nesting), the child is terminated by an
``atexit`` hook instead, which covers a clean exit only; that is the
remaining gap and a warning is logged.
"""
from __future__ import annotations

import atexit
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


def _is_windows() -> bool:
    return sys.platform.startswith("win")


# Windows Job Object constants (winnt.h).
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

_job_lock = threading.Lock()
_job_handle: int | None = None


def _kernel32():
    """The ``kernel32`` DLL. Windows only; tests replace this function."""
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _limit_info_structure():
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` with its flags set to kill on close."""
    import ctypes

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _Extended()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


def _kill_on_close_job() -> int:
    """This process's Job Object that kills its members when the last handle closes. Created once."""
    import ctypes

    global _job_handle
    with _job_lock:
        if _job_handle is not None:
            return _job_handle
        k32 = _kernel32()
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        info = _limit_info_structure()
        if not k32.SetInformationJobObject(
            ctypes.c_void_p(handle), _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            k32.CloseHandle(ctypes.c_void_p(handle))
            raise OSError("SetInformationJobObject failed")
        # Never closed by this code: Windows closes it when the process ends, which kills the members.
        _job_handle = int(handle)
        return _job_handle


def _spawn_windows(cmd: Sequence[str], log_name: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        import ctypes

        job = _kill_on_close_job()
        if not _kernel32().AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(int(proc._handle))):  # type: ignore[attr-defined]
            raise OSError("AssignProcessToJobObject failed")
    except Exception as exc:  # noqa: BLE001 - fall back to cleanup at a clean exit
        logger.warning("could not tie %s to a Windows Job Object (%r); it is stopped at a clean exit only, "
                       "so a crashed edge can leave it running", log_name, exc)

        def _stop(p: subprocess.Popen = proc) -> None:
            if p.poll() is None:
                p.terminate()

        atexit.register(_stop)
    return proc


def spawn_tied_to_parent(cmd: Sequence[str], *, log_name: str = "supervised") -> subprocess.Popen:
    """Start *cmd* so that it is killed when this process dies.

    On POSIX that happens within about a second, through a shell supervisor;
    on Windows through a Job Object (see the module docstring).

    Returns the supervisor's ``Popen``; ``terminate()`` on it stops the
    command too. The command's output is logged at debug level under
    *log_name*.
    """
    if _is_windows():
        proc = _spawn_windows(cmd, log_name)
    else:
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
