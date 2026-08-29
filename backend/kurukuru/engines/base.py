"""
Compute engine abstraction (the pivot point of the architecture).

The rest of the application talks to the hypervisor exclusively through the
:class:`ComputeEngine` interface. Only the concrete drivers know what is really
underneath — ``qemu.py`` drives ``qemu-system-x86_64`` over QMP. No
hypervisor-specific command strings or state names leak past this module.

One driver ships today. The interface is not speculative generality: it already
survived swapping engines once (Multipass was added, then retired, without the
routers or the reconciler changing), and a Hyper-V or HVF driver would slot in
the same way.

Design rules honoured by every driver:
  * Every external command is invoked with an argument *list* (never
    ``shell=True``), with an explicit ``timeout``.
  * The three failure modes that matter — missing binary, timeout, non-zero
    exit — are translated into the exception hierarchy below.
  * Native state vocabularies are mapped to :class:`InstanceStatus` *inside*
    the driver.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from kurukuru.models import InstanceStatus


# --------------------------------------------------------------------------- #
# Exception hierarchy
# --------------------------------------------------------------------------- #
class ComputeEngineError(Exception):
    """Base class for all compute-engine failures.

    Raised (directly) when a hypervisor command exits non-zero. Carries the
    captured ``stderr`` and ``returncode`` so callers can log or surface *why*
    an operation failed.
    """

    def __init__(
        self,
        message: str,
        *,
        stderr: str | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.returncode = returncode


class HypervisorUnavailableError(ComputeEngineError):
    """The hypervisor itself is unreachable — binary missing or daemon down.

    Distinct from :class:`ComputeEngineError` because it is an *environmental*
    fault (retrying the same request won't help) rather than a per-instance
    one; routers map it to HTTP 503.
    """


class ComputeTimeoutError(ComputeEngineError):
    """A hypervisor command exceeded its allotted timeout."""


class InstanceNotFoundError(ComputeEngineError):
    """The requested instance does not exist on the hypervisor."""


# --------------------------------------------------------------------------- #
# Normalised value object
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LaunchOptions:
    """Per-instance launch details that go beyond the flavor.

    Engine-specific by nature: a driver uses what it understands and ignores the
    rest, so adding a capability here never forces a change in drivers that
    can't offer it.
    """

    #: "whpx" | "tcg" | None (let the driver choose). Explicit values matter
    #: because acceleration and console rendering trade off against each other.
    accel: str | None = None
    #: Absolute path to an ISO to attach as a boot CD-ROM (Part B).
    iso_path: str | None = None
    #: Backing image for the copy-on-write overlay; None means the built-in one.
    backing_image: str | None = None
    #: Whether to build a cloud-init seed. False for guests that ignore NoCloud.
    seed_cloud_init: bool = True
    #: Guest display adapter ("std" | "virtio"); None means the driver default.
    display: str | None = None
    #: Guest family ("linux" | "windows"). Drives disk bus, NIC model, display
    #: and CPU model together, because those four have to agree: a Windows
    #: guest given a virtio disk cannot see it, and one given virtio-gpu shows
    #: no picture during setup. Drivers that only ever run one kind of guest
    #: ignore it, like every other field here.
    guest_os: str = "linux"


@dataclass(frozen=True)
class InstanceInfo:
    """Hypervisor-agnostic snapshot of a single instance's actual state.

    ``status`` is ``None`` only when ``exists`` is ``False``. This is what the
    reconciler consumes; it never sees a driver's native state vocabulary.

    The trailing fields are runtime details only some engines have. QEMU fills
    them (it owns the host-side port forwards and the VM process); a driver
    whose hypervisor hands guests their own routable address would leave them
    ``None``, and the corresponding DB columns stay null.
    """

    name: str
    exists: bool
    status: InstanceStatus | None
    ip_address: str | None
    ssh_port: int | None = None
    vnc_port: int | None = None
    qmp_port: int | None = None
    pid: int | None = None
    #: Accelerator the VM is actually running under ("whpx" | "tcg").
    accel: str | None = None
    #: Guest display adapter in use. Surfaced because, together with the
    #: accelerator, it decides whether a text-mode guest can render.
    display: str | None = None
    #: Whether the orchestrator's SSH key was injected. Distinguishes "no
    #: address yet" (a problem) from "never going to have one" (by design).
    ssh_enabled: bool | None = None
    #: Whether the driver's control channel answered. ``None`` for engines that
    #: have no such channel; ``False`` means the VM process is alive but the
    #: monitor did not respond, which is a real state and not a synonym for
    #: stopped — see :meth:`QemuEngine._liveness`.
    monitor_reachable: bool | None = None


@dataclass(frozen=True)
class SnapshotInfo:
    """One snapshot as the hypervisor reports it.

    ``size_bytes`` is what the snapshot's *VM state* occupies, which for a
    stopped-instance qcow2 snapshot is zero — the disk data it preserves is
    accounted for by the overlay growing as blocks diverge, not by a figure the
    hypervisor attributes to the snapshot. Reported anyway rather than hidden,
    because a driver that captures live memory would put a real number here.
    """

    tag: str
    size_bytes: int | None = None
    created_at: datetime | None = None
    #: Whatever the hypervisor calls it internally; QEMU numbers its snapshots.
    hypervisor_id: str | None = None


# --------------------------------------------------------------------------- #
# Abstract interface
# --------------------------------------------------------------------------- #
class ComputeEngine(ABC):
    """Abstract hypervisor driver. Routers depend only on this."""

    #: Registry key of this driver; mirrors ``Instance.engine``.
    name: str = "unknown"

    @abstractmethod
    def is_available(self) -> bool:
        """Return ``True`` iff the hypervisor is installed and responding."""

    @abstractmethod
    def provision_instance(
        self,
        name: str,
        cpus: int,
        memory: str,
        disk: str,
        cloud_init_path: str | None = None,
        options: LaunchOptions | None = None,
    ) -> None:
        """Create and boot a new instance. Blocks until the VM is up.

        ``cloud_init_path`` points at a rendered ``#cloud-config`` document; the
        driver decides how to hand it to the guest (QEMU wraps it in a NoCloud
        seed ISO; another driver might pass it as a launch flag).

        ``options`` carries capabilities only some drivers have (ISO boot,
        accelerator choice, alternate backing image). Drivers must tolerate
        options they cannot honour rather than failing the launch.
        """

    @abstractmethod
    def start_instance(self, name: str) -> None:
        """Power on a stopped instance."""

    @abstractmethod
    def stop_instance(self, name: str) -> None:
        """Gracefully power off a running instance."""

    @abstractmethod
    def destroy_instance(self, name: str) -> None:
        """Delete and purge an instance. Idempotent: absent VM is a no-op."""

    @abstractmethod
    def get_instance_info(self, name: str) -> InstanceInfo:
        """Return the actual state of a single instance."""

    @abstractmethod
    def list_instances(self) -> dict[str, InstanceInfo]:
        """Return actual state of every instance, keyed by name."""

    def describe(self) -> dict[str, object]:
        """Human-facing engine metadata for ``GET /engines``.

        Drivers override to add specifics (e.g. QEMU's acceleration mode).
        """
        return {"name": self.name, "available": self.is_available()}

    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    # Not abstract: a driver with no snapshot support should not be forced to
    # write four stubs, and `supports_snapshots` lets the router refuse with a
    # clear message instead of the caller discovering a NotImplementedError.
    #
    # The state a snapshot captures is deliberately unspecified here. QEMU's
    # are qcow2 internal snapshots taken with the VM stopped, so they hold a
    # disk at rest and no memory; another driver might capture live VM state.
    # What every driver must guarantee is the contract the routes rely on:
    # creating one does not change the guest, and restoring one returns the
    # disk to exactly the state it had when the snapshot was taken.

    #: Whether this driver can clone an instance's disk.
    supports_clone: bool = False

    #: Whether this driver can forward host ports into a guest.
    supports_port_forwards: bool = False

    #: Whether this driver can attach additional disks to an instance.
    supports_volumes: bool = False

    #: Whether this driver can snapshot at all.
    supports_snapshots: bool = False
    #: Whether snapshots can be taken of a *volume*, separately from any
    #: instance. A driver can support one and not the other: instance
    #: snapshots may capture VM state, while a volume is only ever a file.
    supports_volume_snapshots: bool = False

    #: Whether snapshots require the instance to be stopped first. True for
    #: QEMU — see docs/DECISIONS.md for the evidence behind that.
    snapshots_require_stopped: bool = True

    def restart_instance(self, name: str) -> None:
        """Stop and start one instance. Drivers that can do better may."""
        self.stop_instance(name)
        self.start_instance(name)

    def clone_disk(self, source: str, target: str, disk_gb: int | None = None) -> None:
        """Copy a stopped instance's disk into a new instance's directory."""
        raise NotImplementedError

    def add_port_forward(self, name: str, spec: str) -> None:
        """Add a host port forward, applying it live where the driver can."""

    def remove_port_forward(self, name: str, spec: str) -> None:
        """Remove a host port forward, applying it live where the driver can."""

    def set_volumes(self, name: str, paths: list[str]) -> None:
        """Record the ordered volume paths an instance boots with.

        Non-abstract and a no-op by default: a driver whose hypervisor has no
        notion of additional disks should not be forced to implement it, and
        the router's attach route refuses before calling this when
        :attr:`supports_volumes` is False.
        """

    def create_snapshot(self, name: str, tag: str) -> SnapshotInfo:
        """Snapshot instance ``name`` under ``tag``. Returns what was created."""
        raise NotImplementedError(f"{self.name} does not support snapshots")

    def list_snapshots(self, name: str) -> list[SnapshotInfo]:
        """Every snapshot the hypervisor holds for this instance."""
        raise NotImplementedError(f"{self.name} does not support snapshots")

    def restore_snapshot(self, name: str, tag: str) -> None:
        """Return the instance's disk to the state captured by ``tag``."""
        raise NotImplementedError(f"{self.name} does not support snapshots")

    def delete_snapshot(self, name: str, tag: str) -> None:
        """Remove a snapshot. Absent snapshot is not an error."""
        raise NotImplementedError(f"{self.name} does not support snapshots")

    def create_volume_snapshot(self, volume_path: str, tag: str) -> SnapshotInfo:
        raise NotImplementedError

    def list_volume_snapshots(self, volume_path: str) -> list[SnapshotInfo]:
        raise NotImplementedError

    def restore_volume_snapshot(self, volume_path: str, tag: str) -> None:
        raise NotImplementedError

    def delete_volume_snapshot(self, volume_path: str, tag: str) -> None:
        raise NotImplementedError
