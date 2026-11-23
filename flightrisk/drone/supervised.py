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
belongs to a job that cannot nest with ours: nesting fails when either job sets UI limits or the
hierarchy would be invalid), the child is terminated by an
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
import weakref
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
    """A private ``kernel32`` with typed signatures. Windows only; tests replace this function.

    A private ``WinDLL`` keeps our argtypes and restypes off the shared
    ``ctypes.windll.kernel32`` that other libraries use, and ``use_last_error``
    lets a failed call report the Win32 error code.
    """
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32


def _win_error():
    """The ``OSError`` for the last Win32 error of this thread, with its code and message."""
    import ctypes

    return ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]


def _limit_info_type():
    """The ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` structure type (winnt.h)."""
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

    return _Extended


def _limit_info_structure():
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` with its flags set to kill on close."""
    info = _limit_info_type()()
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
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise _win_error()
        info = _limit_info_structure()
        if not k32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info),
        ):
            error = _win_error()
            k32.CloseHandle(handle)
            raise error
        # Never closed by this code: Windows closes it when the process ends, which kills the members.
        _job_handle = int(handle)
        return _job_handle


# Children that only the clean-exit fallback protects. Weak, so a finished
# and dropped Popen is not kept alive; one atexit hook serves them all.
_fallback_procs: "weakref.WeakSet[subprocess.Popen]" = weakref.WeakSet()
_fallback_registered = False


def _stop_fallback_procs() -> None:
    for p in list(_fallback_procs):
        if p.poll() is None:
            p.terminate()


def _protect_at_clean_exit(proc: subprocess.Popen) -> None:
    global _fallback_registered
    with _job_lock:
        _fallback_procs.add(proc)
        if not _fallback_registered:
            atexit.register(_stop_fallback_procs)
            _fallback_registered = True


def _spawn_windows(cmd: Sequence[str], log_name: str) -> subprocess.Popen:
    # The job is made before the child starts, so the only unprotected
    # window is between Popen and the assignment. CREATE_SUSPENDED cannot
    # close it: CPython closes the primary thread handle, so the thread
    # could not be resumed. mavsdk_server starts no children, so nothing
    # can escape the job in that window.
    job: int | None = None
    job_error: Exception | None = None
    try:
        job = _kill_on_close_job()
    except Exception as exc:  # noqa: BLE001 - fall back to cleanup at a clean exit
        job_error = exc
    proc = subprocess.Popen(
        list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if job_error is None:
        try:
            if not _kernel32().AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
                raise _win_error()
        except Exception as exc:  # noqa: BLE001 - fall back to cleanup at a clean exit
            job_error = exc
    if job_error is not None:
        logger.warning("could not tie %s to a Windows Job Object (%s); it is stopped at a clean exit only, "
                       "so a crashed edge can leave it running", log_name, repr(job_error))
        _protect_at_clean_exit(proc)
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
