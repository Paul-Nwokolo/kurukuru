"""
Detached child-process handling for VM processes.

Each platform has its own trap here, and neither is the other's.

Windows:

  * ``os.kill(pid, 0)`` is *not* a liveness probe — CPython maps it onto
    ``TerminateProcess`` for every signal except CTRL_C/CTRL_BREAK, so the
    usual POSIX idiom would kill the VM it was asked about. Liveness goes
    through ``OpenProcess`` + ``GetExitCodeProcess`` instead.
  * QEMU's ``-daemonize`` is POSIX-only. To let VMs outlive a
    ``uvicorn --reload`` restart the child is spawned with DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP and no inherited console/std handles.

POSIX:

  * ``os.kill(pid, 0)`` succeeds for a **zombie**, and every VM here is a
    zombie in waiting — spawned as a child of a backend that never calls
    ``wait()``. Liveness reaps first; see :func:`_posix_pid_alive`.
  * Termination escalates SIGTERM → SIGKILL rather than starting at SIGKILL,
    because QEMU flushes qcow2 metadata on SIGTERM and cannot on SIGKILL.

The detached spawn is ``start_new_session`` (setsid) with the same redirected
std handles, which is the POSIX equivalent of the Windows creation flags.
"""

from __future__ import annotations

import errno
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("kurukuru.qemu.process")

_IS_WINDOWS = sys.platform == "win32"

#: How long a POSIX process gets to honour SIGTERM before SIGKILL. QEMU's own
#: SIGTERM shutdown is near-instant — this is a grace period, not a boot wait.
_TERM_GRACE_SECONDS = 5.0

# POSIX constants, resolved once with fallbacks to their standard values.
# Windows has SIGTERM but neither SIGKILL nor WNOHANG, and the POSIX functions
# below never run there — the fallbacks exist so the *decision logic* in those
# functions can be exercised from any host by substituting the syscalls, rather
# than being untestable everywhere except the platform it ships to.
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)
_WNOHANG = getattr(os, "WNOHANG", 1)

# Windows constants (winbase.h / winnt.h)
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001
_ERROR_ACCESS_DENIED = 5


class ProcessError(Exception):
    """A VM process could not be spawned or signalled."""


def spawn_detached(cmd: list[str], *, log_path: Path, cwd: Path | None = None) -> int:
    """Start ``cmd`` fully detached from this process and return its pid.

    ``log_path`` receives the child's stdout+stderr; without it a failing QEMU
    invocation would be invisible (the guest's own console goes to a separate
    ``-serial`` file). stdin is /dev/null — QEMU must never expect a console.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {}
    if _IS_WINDOWS:
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:  # pragma: no cover - POSIX path, exercised on macOS/Linux hosts
        kwargs["start_new_session"] = True

    logger.info("Spawning detached: %s", " ".join(cmd))
    try:
        # Append: a restarted VM adds to its history rather than erasing it.
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(  # noqa: S603 - list form, never shell=True
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                close_fds=True,
                cwd=str(cwd) if cwd else None,
                **kwargs,
            )
    except FileNotFoundError as exc:
        raise ProcessError(f"Binary not found: {cmd[0]}") from exc
    except OSError as exc:
        raise ProcessError(f"Could not spawn {cmd[0]}: {exc}") from exc

    logger.info("Spawned pid %d (log: %s)", proc.pid, log_path)
    return proc.pid


def pid_alive(pid: int | None) -> bool:
    """True iff a process with ``pid`` currently exists.

    Pid reuse makes this necessary-but-not-sufficient evidence that *our* VM is
    running; callers pair it with a QMP probe.
    """
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        return _win_pid_alive(pid)
    return _posix_pid_alive(pid)


def _posix_pid_alive(pid: int) -> bool:
    """POSIX liveness, with the zombie trap handled first.

    ``os.kill(pid, 0)`` answers "yes" for a **zombie** — a process that has
    exited but whose parent has not collected its status. Every VM here is a
    zombie in waiting: it is spawned as a child of the backend, which then
    throws the ``Popen`` away and never calls ``wait()``. So on Linux a QEMU
    that shut down cleanly would keep reporting alive for as long as the
    backend runs, and ``stop_instance`` would spend its entire 90-second grace
    period waiting for an exit that had already happened, then announce that
    the guest "ignored ACPI powerdown".

    Reaping first fixes it and costs nothing:

    * If ``pid`` is our child and has exited, ``waitpid`` collects it, the pid
      stops existing, and the answer is a truthful False.
    * If it is our child and still running, ``WNOHANG`` returns immediately
      with 0 and we fall through to the real check.
    * If it is not our child at all — the backend restarted and adopted the VM
      from ``runtime.json`` — ``waitpid`` raises ``ChildProcessError`` and we
      fall through. Nothing is at stake there: a process we did not spawn is
      reaped by init, so it can never be a zombie we would misread.

    Windows has no equivalent problem: ``GetExitCodeProcess`` reports a
    terminated process as terminated whether or not anyone collected it.
    """
    try:
        reaped, _status = os.waitpid(pid, _WNOHANG)
        if reaped == pid:
            logger.debug("Reaped exited child pid %d", pid)
            return False
    except ChildProcessError:
        pass  # not our child; init owns its exit status
    except OSError as exc:
        logger.debug("waitpid(%d) failed: %s", pid, exc)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError as exc:
        return exc.errno != errno.ESRCH
    return True


def terminate_pid(pid: int | None) -> bool:
    """Stop ``pid``, escalating politely. Returns True if a signal was sent.

    The last-resort path when a graceful ACPI powerdown times out. "Last
    resort" still does not mean SIGKILL first: QEMU handles ``SIGTERM`` by
    shutting the machine down and flushing its qcow2 metadata, while
    ``SIGKILL`` cannot be handled at all and leaves the image to be repaired on
    next open. So POSIX asks with SIGTERM, waits briefly, and only then insists
    with SIGKILL.

    Windows has no such distinction available: ``TerminateProcess`` is the only
    mechanism that works on a process with no console and no window, and it is
    unconditional — the Windows path is a SIGKILL and is documented as one.
    """
    if not pid or not pid_alive(pid):
        return False
    if _IS_WINDOWS:
        logger.warning("Force-terminating pid %d (TerminateProcess)", pid)
        return _win_terminate(pid)
    return _posix_terminate(pid)


def _posix_terminate(pid: int, *, grace_seconds: float = _TERM_GRACE_SECONDS) -> bool:
    """SIGTERM, a short wait, then SIGKILL if it is still there."""
    logger.warning("Terminating pid %d (SIGTERM)", pid)
    try:
        os.kill(pid, _SIGTERM)
    except OSError as exc:
        logger.warning("Could not SIGTERM pid %d: %s", pid, exc)
        return False

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            logger.info("pid %d exited on SIGTERM", pid)
            return True
        time.sleep(0.2)

    logger.warning("pid %d ignored SIGTERM for %.0fs — SIGKILL", pid, grace_seconds)
    try:
        os.kill(pid, _SIGKILL)
        return True
    except OSError as exc:
        logger.warning("Could not SIGKILL pid %d: %s", pid, exc)
        return False


# --------------------------------------------------------------------------- #
# Windows implementations
# --------------------------------------------------------------------------- #
def _win_pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ACCESS_DENIED means the process exists but belongs to another user;
        # anything else (INVALID_PARAMETER) means it's gone.
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _win_terminate(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        logger.warning(
            "OpenProcess(TERMINATE) failed for pid %d (error %d)",
            pid,
            ctypes.get_last_error(),
        )
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)
