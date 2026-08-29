"""
Process spawning, liveness and termination — including the POSIX paths.

These run on any host. The POSIX branches are driven by substituting the three
syscalls they build on (``waitpid``, ``kill``, and the platform flag), which
tests the *decision tree* — reap before probing, SIGTERM before SIGKILL —
without needing a POSIX kernel underneath.

What that does and does not prove is worth being explicit about, because it is
the difference between a green suite and a working one: it proves the logic
routes correctly given each syscall outcome. It does not prove the kernel
produces those outcomes. That a freshly exited QEMU really does present as a
zombie, and that ``waitpid`` really does clear it, is a Part B observation on a
Linux host. See docs/PORTABILITY.md.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kurukuru.engines import process as process_module
from kurukuru.engines.process import ProcessError, spawn_detached

# The module's own resolved constants, not signal.SIGKILL — Windows has no
# SIGKILL, and these tests deliberately run everywhere.
SIGTERM = process_module._SIGTERM
SIGKILL = process_module._SIGKILL

posix_logic = pytest.mark.posix_logic


@pytest.fixture()
def as_posix(monkeypatch):
    """Route the platform switches down their POSIX branches."""
    monkeypatch.setattr(process_module, "_IS_WINDOWS", False)


# --------------------------------------------------------------------------- #
# Liveness: the zombie trap
# --------------------------------------------------------------------------- #
@posix_logic
def test_an_exited_child_is_reaped_and_reported_dead(as_posix, monkeypatch):
    """The bug this exists to stop: a zombie answering "alive" forever.

    Every VM is spawned as a child of a backend that never calls wait(), so a
    QEMU that shut down cleanly stays a zombie — and a zombie satisfies
    os.kill(pid, 0). Reported alive, stop_instance would burn its whole 90s
    grace period waiting for an exit that already happened.
    """
    calls: list[str] = []

    def fake_waitpid(pid: int, flags: int):
        calls.append("waitpid")
        assert flags == process_module._WNOHANG  # never block the caller
        return (pid, 0)             # collected: it had already exited

    monkeypatch.setattr(process_module.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(
        process_module.os,
        "kill",
        lambda *a: pytest.fail("kill must not be reached once the child is reaped"),
    )

    assert process_module.pid_alive(4242) is False
    assert calls == ["waitpid"]


@posix_logic
def test_a_running_child_is_left_alone_and_reported_alive(as_posix, monkeypatch):
    monkeypatch.setattr(process_module.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(process_module.os, "kill", lambda pid, sig: None)

    assert process_module.pid_alive(4242) is True


@posix_logic
def test_an_adopted_vm_is_probed_not_reaped(as_posix, monkeypatch):
    """After a backend restart the VM is nobody's child; init reaps it.

    waitpid raises ChildProcessError there, which is not an error condition —
    it means the zombie case cannot arise, so fall through to the real probe.
    """
    def fake_waitpid(pid: int, flags: int):
        raise ChildProcessError("not our child")

    monkeypatch.setattr(process_module.os, "waitpid", fake_waitpid)
    monkeypatch.setattr(process_module.os, "kill", lambda pid, sig: None)

    assert process_module.pid_alive(4242) is True


@posix_logic
def test_a_vanished_pid_is_reported_dead(as_posix, monkeypatch):
    def fake_kill(pid: int, sig: int):
        raise ProcessLookupError

    monkeypatch.setattr(process_module.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(process_module.os, "kill", fake_kill)

    assert process_module.pid_alive(4242) is False


@posix_logic
def test_someone_elses_process_counts_as_alive(as_posix, monkeypatch):
    """EPERM means it exists — we simply may not signal it."""
    def fake_kill(pid: int, sig: int):
        raise PermissionError

    monkeypatch.setattr(process_module.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(process_module.os, "kill", fake_kill)

    assert process_module.pid_alive(4242) is True


def test_no_pid_is_never_alive():
    assert process_module.pid_alive(None) is False
    assert process_module.pid_alive(0) is False
    assert process_module.pid_alive(-1) is False


# --------------------------------------------------------------------------- #
# Termination: escalate, don't start at the end
# --------------------------------------------------------------------------- #
@posix_logic
def test_termination_asks_with_sigterm_before_insisting(as_posix, monkeypatch):
    """QEMU flushes qcow2 metadata on SIGTERM and cannot on SIGKILL."""
    signals: list[int] = []
    alive = {"value": True}

    def fake_kill(pid: int, sig: int):
        signals.append(sig)
        if sig == SIGTERM:
            alive["value"] = False  # QEMU handles it and exits

    monkeypatch.setattr(process_module.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(process_module.os, "kill", fake_kill)
    monkeypatch.setattr(process_module, "pid_alive", lambda pid: alive["value"])

    assert process_module._posix_terminate(4242) is True
    assert signals == [SIGTERM]  # SIGKILL never needed


@posix_logic
def test_termination_escalates_to_sigkill_when_ignored(as_posix, monkeypatch):
    """A guest that ignores SIGTERM still has to go, just not first."""
    signals: list[int] = []
    monkeypatch.setattr(process_module.os, "waitpid", lambda pid, flags: (0, 0))
    monkeypatch.setattr(process_module.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(process_module, "pid_alive", lambda pid: True)  # never dies

    assert process_module._posix_terminate(4242, grace_seconds=0.05) is True
    assert signals == [SIGTERM, SIGKILL]


def test_terminating_a_dead_pid_does_nothing():
    assert process_module.terminate_pid(None) is False
    assert process_module.terminate_pid(0) is False


# --------------------------------------------------------------------------- #
# Spawning
# --------------------------------------------------------------------------- #
def test_spawn_detached_runs_the_child_and_captures_its_output(tmp_path: Path):
    """A real child, on whichever platform is running the suite."""
    log = tmp_path / "logs" / "child.log"
    pid = spawn_detached(
        [sys.executable, "-c", "print('hello from the child')"], log_path=log
    )
    assert pid > 0

    # The child writes and exits; give it a moment on a loaded machine.
    for _ in range(50):
        if log.exists() and b"hello" in log.read_bytes():
            break
        __import__("time").sleep(0.1)
    assert b"hello from the child" in log.read_bytes()


def test_spawn_detached_appends_rather_than_truncating(tmp_path: Path):
    """A restarted VM adds to its history instead of erasing it."""
    log = tmp_path / "child.log"
    log.write_bytes(b"previous run\n")
    spawn_detached([sys.executable, "-c", "pass"], log_path=log)
    assert log.read_bytes().startswith(b"previous run")


def test_spawn_detached_reports_a_missing_binary_clearly(tmp_path: Path):
    with pytest.raises(ProcessError, match="Binary not found"):
        spawn_detached(["definitely-not-a-real-binary-xyz"], log_path=tmp_path / "l.log")


@pytest.mark.posix
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session semantics")
def test_spawned_child_leads_its_own_session(tmp_path: Path):
    """setsid is what lets a VM outlive the backend that started it.

    The Windows equivalent (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) cannot
    be asserted this way, so this is the POSIX half of a shared guarantee.
    """
    log = tmp_path / "child.log"
    pid = spawn_detached(
        [sys.executable, "-c", "import os,time; print(os.getsid(0)); time.sleep(2)"],
        log_path=log,
    )
    for _ in range(50):
        if log.exists() and log.read_bytes().strip():
            break
        __import__("time").sleep(0.1)
    session_id = int(log.read_bytes().split()[0])
    assert session_id == pid          # the child is its own session leader
    assert session_id != os.getsid(0)  # and not in ours
    subprocess.run(["kill", "-9", str(pid)], check=False)
