"""
Host capacity detection.

The number that matters is not what the machine *has* but what it can still
*give*: raw totals would happily let a user allocate 16 GB on a 16 GB host and
watch the launch die inside the hypervisor. So every figure here is reported
three ways — total, already committed, and allocatable — and the launch path
validates against the third.

Committed is counted from our own rows rather than from live process memory:
a stopped VM has released its RAM to the host but still owns its allocation as
far as the user is concerned, and a VM that is mid-boot hasn't touched its full
allotment yet. Neither is visible to `psutil`.

Degrades rather than blocks: if psutil is missing or a probe raises, capacity
falls back to permissive limits with a warning. Refusing every launch because a
*capacity check* broke would be a worse failure than the one it prevents.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.models import Instance, InstanceStatus

logger = logging.getLogger("kurukuru.capacity")

try:  # psutil is optional at runtime; see the module docstring.
    import psutil
except ImportError:  # pragma: no cover - exercised by the fallback test
    psutil = None  # type: ignore[assignment]

#: Statuses whose memory is considered spoken for. A Stopped VM has handed its
#: RAM back to the host, so it is deliberately *not* counted — otherwise a user
#: who stops everything still can't launch anything.
_COMMITTED_STATUSES = (
    InstanceStatus.PENDING,
    InstanceStatus.PROVISIONING,
    InstanceStatus.RUNNING,
)

_MB = 1024**2
_GB = 1024**3


@dataclass(frozen=True)
class ResourceCapacity:
    """One resource, reported so the caller can explain a refusal."""

    total: int
    committed: int
    allocatable: int
    #: Largest a single new instance may request.
    max_per_instance: int


@dataclass(frozen=True)
class HostCapacity:
    """What this host can give a new instance right now."""

    cpu: ResourceCapacity            # logical cores
    memory_mb: ResourceCapacity
    disk_gb: ResourceCapacity
    memory_available_mb: int         # what the OS reports free *now*
    accel_available: bool
    accel: str | None
    #: True when psutil is unavailable and the figures are permissive guesses.
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _committed(session: Session) -> tuple[int, int, int]:
    """(cpus, memory_mb, disk_gb) already promised to live instances."""
    rows = session.exec(
        select(Instance).where(Instance.status.in_(_COMMITTED_STATUSES))  # type: ignore[attr-defined]
    ).all()
    return (
        sum(r.cpus or 0 for r in rows),
        sum(r.memory_mb or 0 for r in rows),
        sum(r.disk_gb or 0 for r in rows),
    )


def _disk_free_bytes(settings: Settings) -> tuple[int, int]:
    """(total, free) for the filesystem holding the QEMU working tree.

    Walks up to the nearest existing ancestor: on a fresh install the qemu
    directory hasn't been created yet, and the answer we want is about the
    volume it will live on.
    """
    path = Path(settings.qemu_dir).expanduser()
    while not path.exists() and path != path.parent:
        path = path.parent
    usage = shutil.disk_usage(path)
    return usage.total, usage.free


def compute_capacity(session: Session, settings: Settings | None = None) -> HostCapacity:
    """Measure the host and subtract what we have already promised."""
    settings = settings or get_settings()
    warnings: list[str] = []
    degraded = False

    used_cpus, used_memory_mb, used_disk_gb = _committed(session)

    if psutil is None:
        degraded = True
        warnings.append(
            "psutil is not installed — capacity limits are permissive guesses. "
            "Install it to enforce real host limits."
        )
        cpu_total = 8
        memory_total_mb = 16 * 1024
        memory_available_mb = memory_total_mb
    else:
        try:
            cpu_total = psutil.cpu_count(logical=True) or 1
            vm = psutil.virtual_memory()
            memory_total_mb = vm.total // _MB
            memory_available_mb = vm.available // _MB
        except Exception as exc:  # noqa: BLE001 - a probe must never block launches
            degraded = True
            warnings.append(f"Could not read host resources ({exc}); limits are permissive.")
            cpu_total, memory_total_mb, memory_available_mb = 8, 16 * 1024, 16 * 1024

    try:
        disk_total_bytes, disk_free_bytes = _disk_free_bytes(settings)
    except OSError as exc:
        degraded = True
        warnings.append(f"Could not read free disk space ({exc}); limits are permissive.")
        disk_total_bytes = disk_free_bytes = 512 * _GB

    # --- Memory -----------------------------------------------------------
    # Reserve for the host first, then subtract our own commitments.
    reserve_mb = settings.host_reserve_memory_bytes // _MB
    memory_allocatable = max(0, memory_total_mb - reserve_mb - used_memory_mb)

    # --- CPU --------------------------------------------------------------
    # Oversubscription is normal for vCPUs — they timeshare — so the pool is
    # larger than the core count, but no single VM may claim more than the host
    # physically has, since that buys nothing and confuses the guest.
    cpu_pool = int(cpu_total * settings.cpu_oversubscribe_factor)
    cpu_allocatable = max(0, cpu_pool - used_cpus)
    cpu_max_per_instance = min(cpu_total, cpu_allocatable) if cpu_allocatable else 0

    # --- Disk -------------------------------------------------------------
    # qcow2 overlays are sparse: a 20 GB disk occupies a few hundred MB until
    # written. Committed size is therefore *not* consumed space, so the real
    # bound is current free space and both numbers are reported.
    disk_free_gb = disk_free_bytes // _GB
    disk_total_gb = disk_total_bytes // _GB

    return HostCapacity(
        cpu=ResourceCapacity(
            total=cpu_total,
            committed=used_cpus,
            allocatable=cpu_allocatable,
            max_per_instance=cpu_max_per_instance,
        ),
        memory_mb=ResourceCapacity(
            total=memory_total_mb,
            committed=used_memory_mb,
            allocatable=memory_allocatable,
            max_per_instance=memory_allocatable,
        ),
        disk_gb=ResourceCapacity(
            total=disk_total_gb,
            committed=used_disk_gb,
            allocatable=disk_free_gb,
            max_per_instance=disk_free_gb,
        ),
        memory_available_mb=memory_available_mb,
        accel_available=False,  # filled in by the caller that owns the engine
        accel=None,
        degraded=degraded,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# Short-lived cache
# --------------------------------------------------------------------------- #
_cache: tuple[float, HostCapacity] | None = None


def get_capacity(
    session: Session, settings: Settings | None = None, *, force: bool = False
) -> HostCapacity:
    """Cached capacity. The dashboard polls every 3s; psutil need not."""
    global _cache
    settings = settings or get_settings()
    now = time.monotonic()

    if not force and _cache is not None:
        cached_at, value = _cache
        if now - cached_at < settings.capacity_cache_seconds:
            return value

    value = compute_capacity(session, settings)
    _cache = (now, value)
    return value


def invalidate_cache() -> None:
    """Drop the cached reading — used after a launch changes commitments."""
    global _cache
    _cache = None
