"""The two probes that were being paid for far more often than necessary.

Both came out of one user's log, and they are opposite problems:

* ``is_available`` spawned two QEMU processes *per call*, and a log showed
  ``qemu-system-x86_64 --version`` starting several times a second with the
  dashboard open — five endpoints reach it and several are polled.
* ``_probe_accel`` is paid **once**, and costs 6.0 seconds of an 8.4-second
  cold start, because QEMU started with ``-S`` never exits and the probe's
  *timeout* is therefore its success signal.

Which makes the second one's shape the interesting fact, and the thing these
tests are written to protect:

    A working accelerator is the slow answer. A broken one is instant.

So only a success is cached. Caching a failure would save nothing measurable
and would strand a host whose accelerator had just been enabled.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from kurukuru.engines import accel_cache
from kurukuru.engines.qemu import QemuEngine
from kurukuru.config import Settings


@pytest.fixture()
def engine(tmp_path: Path) -> QemuEngine:
    return QemuEngine(Settings(state_dir=str(tmp_path / "state")))


# --------------------------------------------------------------------------- #
# is_available
# --------------------------------------------------------------------------- #
def test_repeated_availability_checks_run_one_pair_of_probes(engine, monkeypatch):
    """The dashboard's polling must not cost a subprocess per poll."""
    runs: list[list[str]] = []

    def counting_run(cmd, *, timeout):
        runs.append(cmd)
        class Ok:
            returncode = 0
            stdout = "QEMU emulator version 11.1.0"
            stderr = ""
        return Ok()

    monkeypatch.setattr(engine, "_run", counting_run)

    for _ in range(50):
        assert engine.is_available() is True

    assert len(runs) == 2, (
        f"50 availability checks ran {len(runs)} QEMU processes; the cache is "
        f"not holding"
    )


def test_a_failure_is_retried_once_the_window_passes(engine, monkeypatch):
    """A cached *failure* must expire, or recovery needs a restart.

    Someone who allows the binary through Smart App Control, or clears a bad
    override, should see the engine come back on the next poll — not be told
    to restart a backend they have no way to restart.
    """
    from kurukuru.engines.base import ComputeEngineError

    state = {"broken": True}

    def flaky_run(cmd, *, timeout):
        if state["broken"]:
            raise ComputeEngineError("blocked")
        class Ok:
            returncode = 0
            stdout = ""
            stderr = ""
        return Ok()

    monkeypatch.setattr(engine, "_run", flaky_run)
    assert engine.is_available() is False

    # Still inside the window: the cached answer stands.
    state["broken"] = False
    assert engine.is_available() is False

    # Past it: measured again, and recovers without a restart.
    monkeypatch.setattr("kurukuru.engines.qemu._AVAILABILITY_TTL_SECONDS", 0.0)
    assert engine.is_available() is True


def test_the_availability_cache_can_be_dropped_on_demand(engine, monkeypatch):
    from kurukuru.engines.base import ComputeEngineError

    monkeypatch.setattr(
        engine, "_run", lambda cmd, *, timeout: (_ for _ in ()).throw(
            ComputeEngineError("no")
        )
    )
    assert engine.is_available() is False
    engine.invalidate_availability()

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(engine, "_run", lambda cmd, *, timeout: Ok())
    assert engine.is_available() is True


def test_concurrent_callers_share_one_availability_probe(engine, monkeypatch):
    """A burst of polls must cost one pair of probes, not one pair each.

    The endpoints that reach this are polled by a browser and read by the
    reconciler's own thread, so "concurrent" is the ordinary case rather than
    a contrived one.
    """
    import threading

    runs: list[str] = []
    started = threading.Event()

    def slow_run(cmd, *, timeout):
        runs.append(cmd[0])
        started.set()
        time.sleep(0.05)  # wide enough for the others to pile up behind it

        class Ok:
            returncode = 0
            stdout = ""
            stderr = ""

        return Ok()

    monkeypatch.setattr(engine, "_run", slow_run)

    threads = [threading.Thread(target=engine.is_available) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(runs) == 2, (
        f"8 concurrent availability checks ran {len(runs)} QEMU processes; "
        f"they should share one measurement"
    )


def test_concurrent_callers_share_one_acceleration_probe(tmp_path, monkeypatch):
    """The expensive one, and the bug a real install actually showed.

    A single backend start logged three "whpx operational" lines: three
    six-second QEMU processes racing through the same probe, each finding the
    on-disk cache empty because none had finished to write it. The in-process
    memo was a read-modify-write across that whole gap.
    """
    import threading

    settings = Settings(state_dir=str(tmp_path / "state"))
    engine = QemuEngine(settings)
    Path(settings.qemu_dir).expanduser().mkdir(parents=True, exist_ok=True)

    probes: list[str] = []

    def counting_probe():
        probes.append("probe")
        time.sleep(0.05)
        return "whpx"

    monkeypatch.setattr(engine, "_probe_accel", counting_probe)

    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(engine.accel()))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(probes) == 1, (
        f"8 concurrent accel() callers ran {len(probes)} probes; at six "
        f"seconds each that is the cold start this cache exists to remove"
    )
    assert results == ["whpx"] * 8


# --------------------------------------------------------------------------- #
# The accelerator cache
# --------------------------------------------------------------------------- #
def test_a_measured_accelerator_survives_a_restart(tmp_path):
    """The whole point: the 6-second probe is paid once, not once per start."""
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"pretend emulator")

    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None
    accel_cache.store(root, str(binary), "11.1.0", "whpx")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") == "whpx"


def test_a_different_binary_is_measured_afresh(tmp_path):
    """An upgrade must not inherit an answer about the binary it replaced."""
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"old build")

    accel_cache.store(root, str(binary), "11.1.0", "whpx")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") == "whpx"

    # Same path, same reported version, different bytes — which is exactly
    # what a rebuilt or swapped binary looks like, and exactly the mistake
    # this project has made before by trusting a name over the file.
    binary.write_bytes(b"a different build entirely")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None


def test_a_different_version_is_measured_afresh(tmp_path):
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"emulator")

    accel_cache.store(root, str(binary), "11.1.0", "whpx")
    assert accel_cache.load(root, str(binary), lambda: "11.2.0") is None


def test_a_stale_entry_expires(tmp_path, monkeypatch):
    """The backstop for a host whose accelerator was turned off.

    Nothing about the binary changes when Windows Hypervisor Platform is
    disabled, so the key cannot notice; the age is what does.
    """
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"emulator")

    accel_cache.store(root, str(binary), "11.1.0", "whpx")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") == "whpx"

    monkeypatch.setattr(accel_cache, "_TTL_SECONDS", -1)
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None


def test_a_clock_that_moved_backwards_is_treated_as_stale(tmp_path, monkeypatch):
    """A future timestamp must not read as infinitely fresh."""
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"emulator")

    # The real clock, captured before patching — the lambda must not call the
    # name it is replacing.
    real_time = time.time
    monkeypatch.setattr(accel_cache.time, "time", lambda: real_time() + 100_000)
    accel_cache.store(root, str(binary), "11.1.0", "whpx")
    monkeypatch.undo()

    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None


def test_a_damaged_cache_file_means_probe_rather_than_fail(tmp_path):
    """Every way of failing has to land on 'do the work', which is always safe."""
    root = tmp_path / "qemu"
    root.mkdir()
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"emulator")

    (root / "accel-probe.json").write_text("{ not json", encoding="utf-8")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None

    (root / "accel-probe.json").write_text('{"schema": 999}', encoding="utf-8")
    assert accel_cache.load(root, str(binary), lambda: "11.1.0") is None


def test_storing_into_an_unwritable_place_is_not_an_error(tmp_path):
    """A cache that cannot be written is a slow start, never a failure."""
    # A file where the directory should be: mkdir and write both fail.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way", encoding="utf-8")
    accel_cache.store(blocked, "qemu-system-x86_64", "11.1.0", "whpx")
    assert accel_cache.load(blocked, "qemu-system-x86_64", lambda: "11.1.0") is None


def test_only_a_success_is_ever_cached(tmp_path, monkeypatch):
    """The asymmetry this whole design rests on.

    A failed probe returns "tcg" instantly. Storing that would save nothing
    and would keep telling a host that its accelerator does not work after
    somebody enabled it.
    """
    settings = Settings(state_dir=str(tmp_path / "state"))
    engine = QemuEngine(settings)
    root = Path(settings.qemu_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(engine, "native_accel", lambda: "whpx")
    monkeypatch.setattr(engine, "_binary_version", lambda: "11.1.0")

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "whpx not available"

    monkeypatch.setattr(
        "kurukuru.engines.qemu.subprocess.run", lambda *a, **k: Failed()
    )

    assert engine._probe_accel() == "tcg"
    assert not (root / "accel-probe.json").exists(), (
        "a failed acceleration probe was cached; it must be re-derived each "
        "start so a host that gains an accelerator speeds up by itself"
    )
