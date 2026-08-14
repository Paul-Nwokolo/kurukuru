"""
Host capacity detection and the launch-time limits derived from it.

The arithmetic is the point: raw totals would let a user allocate the whole
machine and watch the launch die inside the hypervisor. These tests pin the
subtraction (host reserve, our own commitments) and the deliberate asymmetries —
CPU oversubscribes, memory does not, stopped instances release memory, sparse
disks are bounded by free space rather than committed size.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.config import Settings
from app.host_capacity import compute_capacity, get_capacity, invalidate_cache
from app.models import Instance, InstanceStatus

_MB = 1024**2
_GB = 1024**3


@pytest.fixture()
def session(tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        qemu_dir=str(tmp_path / "qemu"),
        host_reserve_memory_bytes=2 * _GB,
        cpu_oversubscribe_factor=2.0,
    )


def fake_host(cpus=8, total_mb=16384, available_mb=8192, free_gb=500):
    """Patch the host probes so the arithmetic is tested, not this machine."""
    vm = type("VM", (), {"total": total_mb * _MB, "available": available_mb * _MB})()
    return (
        patch("app.host_capacity.psutil.cpu_count", return_value=cpus),
        patch("app.host_capacity.psutil.virtual_memory", return_value=vm),
        patch("app.host_capacity._disk_free_bytes", return_value=(1000 * _GB, free_gb * _GB)),
    )


def capacity(session, settings, **host):
    a, b, c = fake_host(**host)
    with a, b, c:
        return compute_capacity(session, settings)


def add_instance(session, name, *, cpus, memory_mb, disk_gb, status=InstanceStatus.RUNNING):
    session.add(
        Instance(name=name, cpus=cpus, memory_mb=memory_mb, disk_gb=disk_gb, status=status)
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Memory: reserve first, then our own commitments
# --------------------------------------------------------------------------- #
def test_memory_allocatable_holds_back_the_host_reserve(session, settings):
    cap = capacity(session, settings, total_mb=16384)
    # 16384 total - 2048 reserve, nothing committed.
    assert cap.memory_mb.total == 16384
    assert cap.memory_mb.allocatable == 16384 - 2048


def test_memory_allocatable_subtracts_running_instances(session, settings):
    add_instance(session, "a", cpus=1, memory_mb=4096, disk_gb=10)
    cap = capacity(session, settings, total_mb=16384)

    assert cap.memory_mb.committed == 4096
    assert cap.memory_mb.allocatable == 16384 - 2048 - 4096


def test_stopped_instances_release_their_memory(session, settings):
    """A stopped VM has handed its RAM back; counting it would mean a user who
    stopped everything still couldn't launch anything."""
    add_instance(session, "off", cpus=2, memory_mb=8192, disk_gb=10,
                 status=InstanceStatus.STOPPED)
    cap = capacity(session, settings, total_mb=16384)

    assert cap.memory_mb.committed == 0
    assert cap.memory_mb.allocatable == 16384 - 2048


def test_terminated_instances_are_not_counted(session, settings):
    add_instance(session, "gone", cpus=4, memory_mb=8192, disk_gb=10,
                 status=InstanceStatus.TERMINATED)
    assert capacity(session, settings).memory_mb.committed == 0


@pytest.mark.parametrize("status", [InstanceStatus.PENDING, InstanceStatus.PROVISIONING])
def test_instances_still_launching_already_count(session, settings, status):
    """Their memory isn't touched yet, but it is spoken for — otherwise two
    concurrent launches could each pass the check and jointly overcommit."""
    add_instance(session, "booting", cpus=1, memory_mb=4096, disk_gb=10, status=status)
    assert capacity(session, settings).memory_mb.committed == 4096


def test_memory_allocatable_never_goes_negative(session, settings):
    add_instance(session, "huge", cpus=1, memory_mb=99999, disk_gb=10)
    assert capacity(session, settings, total_mb=16384).memory_mb.allocatable == 0


# --------------------------------------------------------------------------- #
# CPU: oversubscribes, but no single VM exceeds the real core count
# --------------------------------------------------------------------------- #
def test_cpu_pool_oversubscribes(session, settings):
    """vCPUs timeshare, so handing out more than the host has is normal."""
    cap = capacity(session, settings, cpus=8)
    assert cap.cpu.total == 8
    assert cap.cpu.allocatable == 16  # 8 * 2.0


def test_a_single_instance_is_capped_at_the_real_core_count(session, settings):
    """More vCPUs than cores buys nothing and confuses the guest."""
    cap = capacity(session, settings, cpus=8)
    assert cap.cpu.max_per_instance == 8


def test_cpu_commitments_shrink_the_pool(session, settings):
    add_instance(session, "busy", cpus=12, memory_mb=1024, disk_gb=10)
    cap = capacity(session, settings, cpus=8)

    assert cap.cpu.committed == 12
    assert cap.cpu.allocatable == 16 - 12
    # ...and the per-instance cap follows the smaller of the two.
    assert cap.cpu.max_per_instance == 4


# --------------------------------------------------------------------------- #
# Disk: sparse overlays mean committed != used
# --------------------------------------------------------------------------- #
def test_disk_is_bounded_by_free_space_not_committed_size(session, settings):
    """A 20 GB qcow2 overlay occupies a few hundred MB until written, so free
    space is the real bound and both figures are reported."""
    add_instance(session, "big", cpus=1, memory_mb=1024, disk_gb=500)
    cap = capacity(session, settings, free_gb=100)

    assert cap.disk_gb.committed == 500      # promised
    assert cap.disk_gb.allocatable == 100    # actually available


# --------------------------------------------------------------------------- #
# Degradation: a broken probe must never block launches
# --------------------------------------------------------------------------- #
def test_missing_psutil_degrades_permissively(session, settings):
    with patch("app.host_capacity.psutil", None):
        cap = compute_capacity(session, settings)

    assert cap.degraded is True
    assert cap.warnings and "psutil" in cap.warnings[0]
    assert cap.memory_mb.allocatable > 0  # permissive, not zero


def test_a_raising_probe_degrades_rather_than_failing(session, settings):
    with patch("app.host_capacity.psutil.cpu_count", side_effect=OSError("boom")):
        cap = compute_capacity(session, settings)

    assert cap.degraded is True
    assert cap.memory_mb.allocatable > 0


def test_unreadable_disk_degrades(session, settings):
    a, b, _ = fake_host()
    with a, b, patch("app.host_capacity._disk_free_bytes", side_effect=OSError("no")):
        cap = compute_capacity(session, settings)
    assert cap.degraded is True


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_capacity_is_cached_between_polls(session, settings):
    invalidate_cache()
    a, b, c = fake_host()
    with a as cpu_count, b, c:
        get_capacity(session, settings)
        get_capacity(session, settings)
        assert cpu_count.call_count == 1  # second call served from cache
    invalidate_cache()


def test_invalidating_the_cache_forces_a_re_probe(session, settings):
    invalidate_cache()
    a, b, c = fake_host()
    with a as cpu_count, b, c:
        get_capacity(session, settings)
        invalidate_cache()
        get_capacity(session, settings)
        assert cpu_count.call_count == 2
    invalidate_cache()
