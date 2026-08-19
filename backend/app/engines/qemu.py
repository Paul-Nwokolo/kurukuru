"""
QEMU compute engine — direct hypervisor control, no CLI wrapper in between.

Layout on disk (all under ``settings.qemu_dir``)::

    base-images/noble-server-cloudimg-amd64.img   shared, downloaded once
    instances/<name>/disk.qcow2                   copy-on-write overlay
    instances/<name>/seed.iso                     NoCloud cloud-init volume
    instances/<name>/runtime.json                 pinned ports + pid
    instances/<name>/qemu.log                     guest serial console
    instances/<name>/qemu-process.log             QEMU's own stdout/stderr

That directory *is* the engine's state. The DB mirrors it (via the reconciler)
for the API's benefit, but a VM can be found, probed, stopped and destroyed with
nothing but the filesystem — which is what lets VMs survive a backend restart.

Liveness is deliberately two-factor: the pid must exist **and** QMP must answer.
A pid alone can be a recycled number; QMP alone can't distinguish "not booted
yet" from "gone". Together they mean *this* VM is running.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from app.config import Settings, get_settings
from app.engines.base import (
    ComputeEngine,
    ComputeEngineError,
    ComputeTimeoutError,
    HypervisorUnavailableError,
    InstanceInfo,
    LaunchOptions,
    SnapshotInfo,
)
from app.engines.capabilities import QemuSupport, cached_support
from app.engines.images import BaseImageError, ensure_base_image
from app.engines.ports import PortAllocationError, allocate_port, is_port_free
from app.engines.process import ProcessError, pid_alive, spawn_detached, terminate_pid
from app.engines.qmp import (
    QmpError,
    hostfwd_add,
    hostfwd_remove,
    is_responsive,
    quit_vm,
    system_powerdown,
)
from app.engines.seed import SeedIsoError, build_meta_data, build_seed_iso
from app.models import InstanceStatus

logger = logging.getLogger("iaas.qemu")

#: Everything QEMU forwards is bound to loopback — this is a local IaaS, and a
#: VM's SSH port has no business being reachable from the LAN.
HOST_IP = "127.0.0.1"

#: The single user-mode netdev every instance has. Named because ``hostfwd_add``
#: and ``hostfwd_remove`` address it by id, and a literal repeated in three
#: places is a typo waiting to remove the wrong forward.
NETDEV_ID = "n0"

#: The one hardware accelerator QEMU can use on each platform. There is no
#: choice to make within a platform — WHPX exists only on Windows, KVM only on
#: Linux, HVF only on macOS — so the platform decides the candidate and the
#: probe decides whether it works. A platform absent from this map has no
#: accelerator and runs under TCG.
HOST_ACCELS = {
    "win32": "whpx",
    "linux": "kvm",
    "darwin": "hvf",
}

#: Accelerators that are hardware-backed. Used to answer "is this accelerated?"
#: — which is *not* the same question as "is this WHPX?", though the two were
#: indistinguishable while Windows was the only supported host.
HARDWARE_ACCELS = frozenset(HOST_ACCELS.values())

#: Budget for the readiness probe in :meth:`QemuEngine.get_instance_info`.
#:
#: The same 5s the boot wait allows, and deliberately not a tighter number of
#: its own. "Immediately over loopback" is an accelerated-host intuition: on a
#: software-emulated guest the banner measured **1.0–1.24s**, because the
#: emulated CPU has to run sshd's accept path. A 1s budget — which looked
#: generous — turned a healthy guest into one with no address, and
#: `launch --wait && ssh` failed on a VM that was serving perfectly.
#:
#: The cost is bounded: a stopped VM refuses the connection instantly, so only
#: an instance that is Running *and still booting* can spend this, and only
#: until it answers.
_READY_PROBE_SECONDS = 5.0

_RUNTIME_FILE = "runtime.json"
_DISK_FILE = "disk.qcow2"
_SEED_FILE = "seed.iso"
_SERIAL_LOG = "qemu.log"
_PROCESS_LOG = "qemu-process.log"


@dataclass
class InstanceRuntime:
    """The per-VM facts that must outlive the backend process.

    Ports are pinned here at provision time: ``start_instance`` re-spawns with
    exactly these, so a stop/start cycle never changes the SSH command a user
    has already copied.
    """

    ssh_port: int
    qmp_port: int
    vnc_port: int
    pid: int | None = None
    cpus: int = 1
    memory: str = "1G"
    accel: str = "tcg"
    #: Absolute path to a boot ISO, when this VM was created from one. Persisted
    #: because start_instance must re-attach the same media on every boot.
    iso_path: str | None = None
    #: Whether this guest received the orchestrator's SSH key. False for ISO
    #: installs and for images with no cloud-init — those are reachable only
    #: through the console, and neither readiness nor the row may imply SSH.
    ssh_enabled: bool = True
    #: Guest display adapter ("std" | "virtio"). Persisted because changing it
    #: across a restart would hand the guest different hardware than the drivers
    #: it bound on first boot. Defaults to std for runtime files written before
    #: the field existed.
    display: str = "std"
    #: Absolute paths of attached volumes, **in attach order**. Persisted here
    #: rather than looked up per boot because this file is what ``start_instance``
    #: replays, and the order is load-bearing: QEMU enumerates virtio-blk
    #: devices in argument order, so the guest's ``/dev/vdb``, ``/dev/vdc`` …
    #: follow this list. Reordering it silently renames the guest's disks, which
    #: would break an ``/etc/fstab`` written against the old names.
    #:
    #: The database is the source of truth for *which* volumes are attached; this
    #: is the engine's replay copy, written by the router on every attach and
    #: detach. Defaults to empty for runtime files written before volumes existed.
    volumes: list[str] = field(default_factory=list)
    #: Extra host port forwards, as QEMU hostfwd specs. Replayed at launch so
    #: a forward added to a running guest survives a restart. The SSH forward
    #: is not in here — it comes from ``ssh_port`` above.
    port_forwards: list[str] = field(default_factory=list)
    #: Which guest family this VM is, and therefore which virtual hardware it
    #: gets. Persisted for the same reason ``display`` is, only more so: the
    #: disk bus and NIC model are what the guest's drivers bound to on first
    #: boot. Handing a Windows install a virtio disk on its second start —
    #: because the runtime file forgot — is an unbootable VM, not a slow one.
    #: Defaults to "linux" for runtime files written before Windows existed.
    guest_os: str = "linux"

    @property
    def vnc_display(self) -> int:
        """QEMU's ``-vnc :N`` display number (port 5900+N)."""
        return self.vnc_port - 5900


@dataclass(frozen=True)
class GuestProfile:
    """The virtual hardware one guest family can actually drive.

    Windows Setup ships inbox drivers for a specific, conservative set of
    devices and nothing else. Boot it on the paravirtualised hardware Linux
    prefers and the installer reaches "no drives found" — not an error message
    about drivers, just an empty list where the disk should be.

    So the choice is per guest family, and it is data rather than a chain of
    ``if guest_os ==`` scattered through the command builder: every device that
    differs is named here once, which is what stops a later device from being
    added for Linux and silently inherited by Windows.
    """

    #: How the root disk and volumes are attached. "virtio" is the fast path;
    #: "ahci" emits an ICH9 SATA controller Windows has a driver for.
    disk_bus: str
    #: NIC device model. e1000e is an Intel 82574L — inbox on every Windows
    #: since 7, where virtio-net needs the virtio-win ISO.
    nic_model: str
    #: Display adapters this guest can use, best first. Windows Setup has no
    #: virtio-gpu driver, so "std" is not merely the default there, it is the
    #: only one that shows a picture — which is why this is a list to choose
    #: from rather than a default to override.
    displays: tuple[str, ...]
    #: Whether a NoCloud seed means anything to this guest. Windows consumes
    #: none of cloud-init's cloud-config, so it gets no seed at all.
    supports_cloud_init: bool
    #: Whether to attach a USB HID keyboard and tablet instead of relying on the
    #: implicit PS/2 pair. Measured: with ``-display none`` and no VNC client, a
    #: QMP ``send-key`` never reaches the PS/2 keyboard — an entire Windows Setup
    #: key sequence was sent blind and the disk never grew a byte, while the same
    #: sequence with USB HID attached drove Setup screen by screen. The tablet is
    #: here for the same reason plus a second one: PS/2 is a *relative* pointer,
    #: so a guest that never sees a mouse-move origin cannot be clicked
    #: accurately, which makes Setup and the Windows desktop painful to drive.
    #: Absolute coordinates remove that entirely.
    usb_input: bool

    @property
    def default_display(self) -> str:
        return self.displays[0]

    def display_or_default(self, requested: str | None) -> str:
        """Honour a display request only if this guest can actually drive it."""
        return requested if requested in self.displays else self.default_display


#: Linux keeps exactly what it had. The Windows row is the whole of Phase 13's
#: device story, and every entry in it is a Windows-inbox-driver decision.
GUEST_PROFILES: dict[str, GuestProfile] = {
    "linux": GuestProfile(
        disk_bus="virtio",
        nic_model="virtio-net-pci",
        displays=("std", "virtio"),
        supports_cloud_init=True,
        # Linux keeps PS/2. These guests are reached over SSH, the console is a
        # fallback, and adding devices would change the hardware under every
        # instance already on disk for no measured gain.
        usb_input=False,
    ),
    "windows": GuestProfile(
        disk_bus="ahci",
        nic_model="e1000e",
        displays=("std",),
        supports_cloud_init=False,
        # Windows has no SSH and no cloud-init: the console is the only way in,
        # and without USB HID it is not actually usable. Windows carries inbox
        # drivers for xHCI and USB HID, so this costs nothing at install time.
        usb_input=True,
    ),
}


#: CPU model for a Windows guest under WHPX. The oldest named model carrying
#: SSE4.2/POPCNT, which Windows 11 and Server 2025 require and ``qemu64`` lacks.
#: See :meth:`QemuEngine.cpu_model` for the measurements behind the choice.
WINDOWS_WHPX_CPU = "Westmere"


def guest_profile(guest_os: str | None) -> GuestProfile:
    """The hardware profile for a guest family, defaulting to Linux.

    An unknown value resolves to Linux rather than raising: this is read from a
    runtime file that may predate the field, and the pre-Windows default is
    exactly what those VMs were built with.
    """
    return GUEST_PROFILES.get((guest_os or "linux").lower(), GUEST_PROFILES["linux"])


#: ``qemu-img snapshot -l`` output, e.g.
#:   ID  TAG        VM_SIZE      DATE            VM_CLOCK   ICOUNT
#:   1   clean       0 B  2026-08-12 00:10:29  0000:00:00.000  0
#: Parsed positionally rather than by column offset: the widths shift with the
#: longest tag, and tags may contain spaces only at the ends (qemu-img strips
#: them), so the fixed head and tail are what can be relied on.
_SNAPSHOT_ROW = re.compile(
    r"^(?P<id>\S+)\s+(?P<tag>.+?)\s+(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?i?B)\s+"
    r"(?P<date>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
)

_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4}


def _parse_snapshot_list(output: str) -> list[SnapshotInfo]:
    """Turn ``qemu-img snapshot -l`` text into value objects.

    Text parsing because qemu-img's ``--output=json`` covers ``info``, not
    ``snapshot``. ``qemu-img info --output=json`` does carry a ``snapshots``
    array, but reading it means running a second command over the same file to
    learn what the first already printed.
    """
    snapshots: list[SnapshotInfo] = []
    for line in output.splitlines():
        match = _SNAPSHOT_ROW.match(line.strip())
        if not match:
            continue  # header, separator, or the "Snapshot list:" banner
        size = float(match["size"]) * _SIZE_UNITS.get(match["unit"], 1)
        try:
            created = datetime.strptime(match["date"], "%Y-%m-%d %H:%M:%S").astimezone(
                timezone.utc
            )
        except ValueError:  # pragma: no cover - qemu-img's format is stable
            created = None
        snapshots.append(
            SnapshotInfo(
                tag=match["tag"].strip(),
                size_bytes=int(size),
                created_at=created,
                hypervisor_id=match["id"],
            )
        )
    return snapshots


class QemuEngine(ComputeEngine):
    """Concrete :class:`ComputeEngine` driving ``qemu-system-x86_64`` over QMP."""

    name = "qemu"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._system_binary = self._settings.qemu_system_binary
        self._img_binary = self._settings.qemu_img_binary
        self._accel: str | None = None  # probed lazily, then cached

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    @property
    def _root(self) -> Path:
        return Path(self._settings.qemu_dir).expanduser()

    @property
    def _instances_dir(self) -> Path:
        return self._root / "instances"

    def _dir(self, name: str) -> Path:
        return self._instances_dir / name

    # ------------------------------------------------------------------ #
    # Runtime state file
    # ------------------------------------------------------------------ #
    def _read_runtime(self, name: str) -> InstanceRuntime | None:
        path = self._dir(name) / _RUNTIME_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable runtime file for '%s': %s", name, exc)
            return None
        try:
            return InstanceRuntime(**data)
        except TypeError as exc:  # schema drift from an older build
            logger.warning("Ignoring incompatible runtime file for '%s': %s", name, exc)
            return None

    def _write_runtime(self, name: str, runtime: InstanceRuntime) -> None:
        path = self._dir(name) / _RUNTIME_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(asdict(runtime), indent=2), encoding="utf-8")
        except OSError as exc:
            raise ComputeEngineError(f"Could not persist runtime state for '{name}': {exc}") from exc

    def _reserved_ports(self, exclude: str | None = None) -> set[int]:
        """Every port already spoken for by another instance, running or not."""
        reserved: set[int] = set()
        if not self._instances_dir.exists():
            return reserved
        for child in self._instances_dir.iterdir():
            if not child.is_dir() or child.name == exclude:
                continue
            runtime = self._read_runtime(child.name)
            if runtime is not None:
                reserved.update({runtime.ssh_port, runtime.qmp_port, runtime.vnc_port})
        return reserved

    # ------------------------------------------------------------------ #
    # qemu-img / binary plumbing
    # ------------------------------------------------------------------ #
    def _run(self, cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        """Run a short-lived QEMU tool command (never ``shell=True``).

        .. warning::

           **Any ``qemu-img`` command that writes to an instance's overlay must
           first check that the instance is not running** — call
           :meth:`_refuse_if_running`. Nothing below this line will stop you.

           The natural assumption is that qemu-img refuses to open a disk a
           live QEMU holds. That is true on Linux, where QEMU takes an OFD lock
           and qemu-img declines with "Failed to get shared write lock". It is
           **not true on Windows**, measured on this project's reference
           platform with a VM running and its overlay open:

               qemu-img info         -> succeeded
               qemu-img snapshot -c  -> succeeded, and wrote the snapshot

           No lock, no warning, two writers on one qcow2. So the engine's own
           guard is not a second opinion that duplicates the hypervisor's — on
           Windows it is the only protection there is, and a new call path that
           skips it corrupts disks on the platform most of this project's users
           are on. See docs/DECISIONS.md #15.

           Read-only commands (``info``, ``snapshot -l``, ``--version``) are
           fine unguarded.
        """
        logger.debug("exec: %s (timeout=%ss)", " ".join(cmd), timeout)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise HypervisorUnavailableError(
                f"QEMU binary '{cmd[0]}' not found — is QEMU installed and on PATH?"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ComputeTimeoutError(f"'{cmd[0]}' timed out after {timeout}s") from exc

        if proc.returncode != 0:
            stderr = (proc.stderr or proc.stdout or "").strip()
            raise ComputeEngineError(
                f"'{Path(cmd[0]).name} {cmd[1] if len(cmd) > 1 else ''}' "
                f"failed (exit {proc.returncode}): {stderr}",
                stderr=stderr,
                returncode=proc.returncode,
            )
        return proc

    # ------------------------------------------------------------------ #
    # Acceleration probe
    # ------------------------------------------------------------------ #
    #: Accelerators that don't give QEMU the memory dirty-tracking its VGA
    #: emulation relies on. The effect is narrower than it first appears: only a
    #: guest sitting in **VGA text mode** comes up blank. A guest that does a KMS
    #: modeset to a linear framebuffer (any ISO installer worth the name) renders
    #: fine, and virtio-gpu renders regardless because the guest driver pushes
    #: updates over a virtqueue instead of QEMU polling memory.
    ACCELS_WITHOUT_DIRTY_TRACKING = frozenset({"whpx"})

    @classmethod
    def console_caveat(cls, accel: str | None, display: str | None) -> str | None:
        """Warn about the one combination that can legitimately render blank.

        Returns None when the console is expected to work. This is advisory, not
        a gate: whether a guest leaves VGA text mode is a property of the guest,
        not of anything we can see from here, so the honest move is to open the
        console and explain the possibility rather than refuse.
        """
        if accel in cls.ACCELS_WITHOUT_DIRTY_TRACKING and (display or "std") == "std":
            return (
                "Guests that stay in VGA text mode (notably the Ubuntu cloud "
                "image) render nothing on standard graphics under hardware "
                "acceleration. If the screen stays black, relaunch with modern "
                "graphics — or software emulation."
            )
        return None

    def resolve_accel(self, requested: str | None, guest_os: str | None = None) -> str:
        """Choose the accelerator for one VM.

        Hardware acceleration is the default for every boot mode. The rule that
        forced ISO instances onto software emulation is gone: it was based on
        the belief that accelerated VMs could not render a console at all, which
        turned out to be true only for VGA-text-mode guests — and an ISO
        installer is precisely the kind of guest that switches to a framebuffer.

        ``guest_os`` is accepted so a guest family *can* influence this, but no
        family currently does, and that is a deliberate retraction rather than
        an oversight. A Windows-specific TCG default was added here on the
        strength of "WHPX stalls, TCG progresses" and removed once longer runs
        showed TCG stalling too — a little further along, and still without
        writing a byte to disk. Defaulting Windows to software emulation would
        therefore have bought a ~30x slowdown for no working install. See
        docs/DECISIONS.md on the Windows boot investigation.
        """
        available = self.accel()
        if requested == "tcg":
            return "tcg"

        if requested in HARDWARE_ACCELS:
            if requested != available:
                logger.warning(
                    "%s requested but %s is what this host offers; using %s",
                    requested,
                    available,
                    available,
                )
                return available
            return requested
        return available

    def accel(self) -> str:
        """The acceleration backend this host can actually use.

        Probed once per process, and the candidate is chosen by platform: each
        OS has exactly one hardware accelerator QEMU can use, and asking for
        another host's is guaranteed to fail. Falling back to TCG keeps the
        tool alive on a host with no accelerator — just very slowly — rather
        than hard-failing every launch.
        """
        if self._accel is None:
            self._accel = self._probe_accel()
        return self._accel

    def native_accel(self) -> str | None:
        """The hardware accelerator this platform *could* use, before probing.

        None on a platform with no supported accelerator, which means TCG is
        not a fallback there but the only answer.
        """
        return HOST_ACCELS.get(sys.platform)

    def _probe_accel(self) -> str:
        """Ask QEMU whether the platform's accelerator is usable, once."""
        candidate = self.native_accel()
        if candidate is None:
            logger.warning(
                "No hardware accelerator is supported on platform '%s' — using TCG "
                "software emulation; VMs will boot roughly 30x slower",
                sys.platform,
            )
            return "tcg"

        # A missing or unreadable /dev/kvm is the overwhelmingly common Linux
        # cause, and QEMU's own error for it ("Could not access KVM kernel
        # module: Permission denied") is easy to miss in a log. Check first so
        # the reason is stated in terms of the fix.
        blocked = self._kvm_unavailable_reason() if candidate == "kvm" else None
        if blocked:
            logger.warning("Acceleration probe: %s — falling back to TCG", blocked)
            return "tcg"

        cmd = [
            self._system_binary,
            "-machine", "q35",
            "-accel", self._accel_arg(candidate),
            "-display", "none",
            "-m", "128",
            "-S",              # start paused: nothing ever executes
            "-no-user-config",
        ]
        try:
            # QEMU started with -S sits there forever, so the *timeout* is the
            # success signal: an unavailable accelerator makes QEMU exit at once.
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=6, check=False)
        except subprocess.TimeoutExpired:
            logger.info("Acceleration probe: %s operational", candidate)
            return candidate
        except FileNotFoundError:
            logger.warning("Acceleration probe: '%s' not found", self._system_binary)
            return "tcg"

        logger.warning(
            "Acceleration probe: %s unavailable (exit %d: %s) — falling back to "
            "TCG software emulation; VMs will boot slowly",
            candidate,
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:300],
        )
        return "tcg"

    @staticmethod
    def _kvm_unavailable_reason() -> str | None:
        """Why /dev/kvm can't be used, in the user's terms — or None if it can.

        Permission is the usual answer and it has a specific remedy, so it is
        worth distinguishing from "no virtualization on this machine".
        """
        device = Path("/dev/kvm")
        if not device.exists():
            return (
                "/dev/kvm does not exist — this host has no KVM support, or "
                "virtualization is disabled in its BIOS/UEFI (or it is itself a "
                "VM without nested virtualization)"
            )
        if not os.access(device, os.R_OK | os.W_OK):
            return (
                "/dev/kvm exists but is not readable/writable by this user — add "
                "the account running the backend to the 'kvm' group and log back in"
            )
        return None

    @staticmethod
    def _accel_arg(accel: str) -> str:
        """QEMU's ``-accel`` argument for one of our accelerator names.

        WHPX is the only one needing an option: its in-kernel IRQ chip is not
        implemented, so ``kernel-irqchip=off`` is mandatory. KVM and HVF take
        their defaults, and anything unrecognised falls back to TCG rather than
        being passed through to QEMU as an invalid accelerator name.
        """
        if accel == "whpx":
            return "whpx,kernel-irqchip=off"
        return accel if accel in ("kvm", "hvf", "tcg") else "tcg"

    def cpu_model(self, accel: str | None = None, guest_os: str | None = None) -> str:
        """Guest CPU model appropriate to the accelerator *and* the guest.

        Three answers by accelerator, for three different reasons:

        * **WHPX — a conservative model.** WHPX cannot virtualize everything
          ``-cpu max`` and ``-cpu host`` advertise: the guest takes an
          unsupported exit early in kernel boot and QEMU dies with "WHPX:
          Unexpected VP exit code 4" before a single serial line is written.
          (Reproduced on this host with both ``max`` and
          ``max,vmx=off,svm=off``.)
        * **KVM/HVF — host.** Passing the physical CPU through is both correct
          and materially faster: the guest gets AES-NI, AVX and the rest instead
          of emulated substitutes. The WHPX workaround must not leak here — it
          would silently hobble every Linux guest for a Windows bug.
        * **TCG — max.** Nothing is being virtualized, so the emulator may as
          well advertise the richest model it can implement.

        **And then the guest gets a say, which is a Phase 13 correction.** The
        Phase 5 decision recorded "WHPX means qemu64" as though the accelerator
        were the only input. It is not, and the difference is load-bearing:
        ``qemu64`` does not expose SSE4.2 or POPCNT, and **Windows 11 and
        Windows Server 2025 refuse to run without them.** A Windows guest on
        ``qemu64`` is not slow, it is unbootable.

        The narrower fact behind the Phase 5 rule is that only the *host-derived*
        models break WHPX. Measured on this host at QEMU 10.0.94, each surviving
        an 18-second run: ``Nehalem``, ``Westmere``, ``SandyBridge``,
        ``Skylake-Client`` and ``qemu64,+sse4.2,+popcnt`` all start; only ``max``
        and ``host`` still die. ``Westmere`` is the pick — the oldest model that
        carries SSE4.2, so it asks the accelerator for the least while still
        clearing the bar Windows sets.

        Linux keeps ``qemu64`` under WHPX exactly as before. Nothing about a
        Windows requirement should change what already boots.
        """
        if self._settings.qemu_cpu_model:
            return self._settings.qemu_cpu_model
        resolved = accel or self.accel()
        if resolved == "whpx":
            return WINDOWS_WHPX_CPU if (guest_os or "linux").lower() == "windows" else "qemu64"
        if resolved in ("kvm", "hvf"):
            return "host"
        return "max"

    # ------------------------------------------------------------------ #
    # Command construction (pure — unit tested without touching QEMU)
    # ------------------------------------------------------------------ #
    def build_overlay_command(
        self, name: str, disk: str, backing: Path | None = None
    ) -> list[str]:
        """``qemu-img create`` argv for a copy-on-write overlay on a base image."""
        base = backing or self._base_image_path()
        return [
            self._img_binary,
            "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(base),
            str(self._dir(name) / _DISK_FILE),
            disk,
        ]

    def build_blank_disk_command(self, name: str, disk: str) -> list[str]:
        """``qemu-img create`` argv for an empty disk with no backing file.

        What an ISO installs *onto*. No ``-b``/``-F``: a backing file would put
        someone else's filesystem underneath the installer.
        """
        return [
            self._img_binary,
            "create",
            "-f", "qcow2",
            str(self._dir(name) / _DISK_FILE),
            disk,
        ]

    def create_blank_disk(self, path: Path, size_gb: int) -> None:
        """Allocate an empty qcow2 at ``path``. Used for volumes.

        Takes an absolute path rather than an instance name, because a volume
        is not owned by an instance — it outlives the ones it is attached to,
        which is the entire point of the feature.
        """
        self._run(
            [self._img_binary, "create", "-f", "qcow2", str(path), f"{size_gb}G"],
            timeout=self._settings.qemu_snapshot_timeout_seconds,
        )

    @staticmethod
    def _ahci_disk(path: Path, index: int) -> list[str]:
        """One qcow2 attached to port ``index`` of the ``ahci`` controller.

        Two arguments per disk rather than one: ``if=virtio`` is a shorthand
        QEMU expands itself, but a SATA disk needs the drive and the device
        stated separately so the device can name which port it sits on. The
        port number is what fixes the guest's disk ordering, the same way the
        argument order does for virtio.
        """
        return [
            "-drive", f"file={path},if=none,id=hd{index},format=qcow2",
            "-device", f"ide-hd,bus=ahci.{index},drive=hd{index}",
        ]

    def build_launch_command(self, name: str, runtime: InstanceRuntime) -> list[str]:
        """Full ``qemu-system-x86_64`` argv for one VM.

        User-mode ("SLIRP") networking is what makes this work with zero host
        configuration: the guest gets NAT'd outbound access for cloud-init's apt
        run, and the single ``hostfwd`` rule is the only way in.
        """
        directory = self._dir(name)
        profile = guest_profile(runtime.guest_os)
        cmd = [
            self._system_binary,
            "-name", name,
            "-machine", "q35",
            "-accel", self._accel_arg(runtime.accel),
            "-cpu", self.cpu_model(runtime.accel, runtime.guest_os),
            # -vga virtio is the single-device form: virtio-gpu for the
            # guest plus VGA compatibility for firmware, rather than a
            # separate -device with -vga none.
            "-vga", profile.display_or_default(runtime.display),
            "-smp", str(runtime.cpus),
            "-m", runtime.memory,
        ]

        if profile.disk_bus == "ahci":
            # Windows: one ICH9 SATA controller, then every disk as a numbered
            # port on it. The controller is emitted once and before any drive,
            # because a `bus=ahci.N` reference has to resolve to something that
            # already exists on the command line.
            cmd += ["-device", "ich9-ahci,id=ahci"]
            cmd += self._ahci_disk(directory / _DISK_FILE, index=0)
            for port, volume_path in enumerate(runtime.volumes, start=1):
                cmd += self._ahci_disk(Path(volume_path), index=port)
        else:
            cmd += [
                "-drive", f"file={directory / _DISK_FILE},if=virtio,format=qcow2",
            ]
            # Attached volumes, in the order the runtime file records. Emitted
            # immediately after the root disk and before any CD-ROM so the guest's
            # virtio-blk numbering is a direct function of this list.
            for volume_path in runtime.volumes:
                cmd += ["-drive", f"file={volume_path},if=virtio,format=qcow2"]

        if runtime.iso_path:
            # Boot order dc = CD first, then disk: the installer runs on the
            # first boot and the installed system takes over once the guest
            # stops finding a bootable CD. menu=on leaves a manual override.
            if profile.disk_bus == "ahci":
                # The installer medium goes on the SATA controller too, on the
                # port after the last data disk. Windows Setup enumerates it
                # with the same inbox driver it uses for the target disk, which
                # is the entire point of putting both on AHCI.
                cd_port = 1 + len(runtime.volumes)
                cmd += [
                    "-drive",
                    f"file={runtime.iso_path},if=none,id=cd0,media=cdrom,readonly=on",
                    "-device", f"ide-cd,bus=ahci.{cd_port},drive=cd0",
                ]
            else:
                cmd += ["-drive", f"file={runtime.iso_path},media=cdrom"]
            cmd += ["-boot", "order=dc,menu=on"]
        elif runtime.ssh_enabled and profile.supports_cloud_init:
            # Cloud image: the NoCloud seed is the only extra medium. Attached
            # only when one was actually generated — an image without cloud-init
            # gets no seed, and QEMU refuses to start if told to open a CD-ROM
            # file that doesn't exist.
            cmd += ["-drive", f"file={directory / _SEED_FILE},media=cdrom"]

        if profile.usb_input:
            # The controller is emitted before the devices that reference it,
            # for the same reason the AHCI controller is: `bus=xhci.0` has to
            # resolve to something already on the command line.
            cmd += [
                "-device", "qemu-xhci,id=xhci",
                "-device", "usb-kbd,bus=xhci.0",
                "-device", "usb-tablet,bus=xhci.0",
            ]

        # The SSH forward is built from the pinned port and always comes
        # first. It is deliberately NOT part of the port-forward table: every
        # instance since Phase 5 depends on it, and putting it behind a
        # migration would risk launching one nobody can reach (DECISIONS #25).
        netdev = f"user,id={NETDEV_ID},hostfwd=tcp:{HOST_IP}:{runtime.ssh_port}-:22"
        # Extra forwards replayed from the runtime file, so a restart restores
        # exactly what was added live.
        for spec in runtime.port_forwards:
            netdev += f",hostfwd={spec}"

        cmd += [
            "-netdev", netdev,
            "-device", f"{profile.nic_model},netdev={NETDEV_ID}",
            "-qmp", f"tcp:{HOST_IP}:{runtime.qmp_port},server,nowait",
            "-vnc", f"{HOST_IP}:{runtime.vnc_display}",
            "-display", "none",
            "-serial", f"file:{directory / _SERIAL_LOG}",
        ]
        return cmd

    def _base_image_path(self) -> Path:
        from app.engines.images import base_image_path

        return base_image_path(self._settings)

    # ------------------------------------------------------------------ #
    # ComputeEngine interface
    # ------------------------------------------------------------------ #
    def is_available(self) -> bool:
        try:
            self._run([self._system_binary, "--version"], timeout=self._settings.cli_timeout_seconds)
            self._run([self._img_binary, "--version"], timeout=self._settings.cli_timeout_seconds)
            return True
        except ComputeEngineError as exc:
            logger.warning("QEMU unavailable: %s", exc)
            return False

    def version(self) -> str | None:
        """The installed build's version string, or None if it cannot be read.

        Stamped onto every instance at launch, so "it worked before the host was
        upgraded" is a checkable claim rather than a hunch.
        """
        support = self.support()
        return support.version.text if support.version else None

    def support(self) -> QemuSupport:
        """Version and capability report for the configured binary.

        Cached: the probes spawn subprocesses, and ``/health`` is polled.
        """
        return cached_support(self._settings, self.accel())

    def describe(self) -> dict[str, object]:
        available = self.is_available()
        support = self.support() if available else None
        return {
            "name": self.name,
            "available": available,
            "version": support.version.text if support and support.version else None,
            "support": support.as_dict() if support else None,
            "acceleration": self.accel() if available else None,
            # "accelerated" means hardware-backed, not "is WHPX". The two were
            # the same statement while Windows was the only supported host, and
            # conflating them made every KVM guest report as unaccelerated.
            "accelerated": self.accel() in HARDWARE_ACCELS if available else False,
            "cpu_model": self.cpu_model() if available else None,
            "base_image": str(self._base_image_path()),
            "base_image_present": self._base_image_path().exists(),
        }

    def provision_instance(
        self,
        name: str,
        cpus: int,
        memory: str,
        disk: str,
        cloud_init_path: str | None = None,
        options: LaunchOptions | None = None,
    ) -> None:
        """Create the disk (+ seed), boot the VM, and wait until it is usable.

        "Usable" differs by boot source. A cloud image is ready when its SSH
        port answers; an ISO has no SSH and no injected key, so the VM is ready
        as soon as QEMU is running and QMP responds — the user takes over from
        the console.
        """
        options = options or LaunchOptions()
        from_iso = bool(options.iso_path)
        profile = guest_profile(options.guest_os)
        # Windows reads none of cloud-init's cloud-config — no shell, no sudoers,
        # no apt — so it gets no seed at all, exactly as an ISO install does.
        # Asked as a property of the guest rather than trusted from the caller,
        # so a Windows launch cannot be handed a Linux-shaped seed by mistake.
        seed_cloud_init = options.seed_cloud_init and profile.supports_cloud_init

        directory = self._dir(name)
        if directory.exists():
            # A leftover directory from a failed run would silently reuse a
            # half-built disk; start from a clean slate instead.
            logger.warning("Instance dir for '%s' already exists — recreating", name)
            self._remove_dir(name)
        directory.mkdir(parents=True, exist_ok=True)

        # 1. The disk. An ISO install needs somewhere empty to install *to*;
        #    a cloud image needs a copy-on-write overlay on its backing file.
        try:
            if from_iso:
                self._run(
                    self.build_blank_disk_command(name, disk),
                    timeout=self._settings.cli_timeout_seconds,
                )
            else:
                backing = Path(options.backing_image) if options.backing_image else None
                if backing is None:
                    backing = ensure_base_image(self._settings)
                elif not backing.exists():
                    raise ComputeEngineError(f"Backing image not found: {backing}")
                logger.info("Using backing image %s for '%s'", backing, name)
                self._run(
                    self.build_overlay_command(name, disk, backing=backing),
                    timeout=self._settings.cli_timeout_seconds,
                )
        except BaseImageError as exc:
            self._remove_dir(name)
            raise ComputeEngineError(str(exc)) from exc
        except ComputeEngineError:
            self._remove_dir(name)
            raise

        # 2. NoCloud seed — only for guests that will actually read it. A
        #    generic ISO ignores it, and attaching a second CD-ROM would only
        #    confuse the boot order.
        if seed_cloud_init and not from_iso:
            try:
                self._write_seed(name, cloud_init_path)
            except SeedIsoError as exc:
                self._remove_dir(name)
                raise ComputeEngineError(str(exc)) from exc

        # 3. Pin the three host ports for this instance's lifetime.
        try:
            runtime = self._allocate_runtime(
                name,
                cpus=cpus,
                memory=memory,
                accel=self.resolve_accel(options.accel, options.guest_os),
                iso_path=options.iso_path,
                ssh_enabled=seed_cloud_init,
                display=options.display or "std",
                guest_os=options.guest_os,
            )
        except PortAllocationError as exc:
            self._remove_dir(name)
            raise ComputeEngineError(f"Port allocation failed for '{name}': {exc}") from exc
        self._write_runtime(name, runtime)

        # 4. Boot and wait for the readiness signal appropriate to this guest.
        self._spawn(name, runtime)
        try:
            # Readiness follows access, not boot source: a guest we injected a
            # key into is ready when SSH answers; one we didn't is ready as soon
            # as the hypervisor is running it, because nothing else is promised.
            if runtime.ssh_enabled:
                elapsed = self._wait_for_ssh(
                    name, runtime, self._settings.qemu_boot_timeout_seconds
                )
            else:
                elapsed = self._wait_for_qmp(name, runtime)
        except ComputeEngineError:
            self._force_off(name, runtime)
            raise
        logger.info(
            "Instance '%s' up in %.1fs (accel=%s, source=%s, ssh=%s:%d)",
            name, elapsed, runtime.accel, "iso" if from_iso else "image",
            HOST_IP, runtime.ssh_port,
        )

    def start_instance(self, name: str) -> None:
        runtime = self._require_runtime(name)
        if self._is_running(runtime):
            logger.info("Instance '%s' is already running", name)
            return

        # The pinned ports are part of the instance's identity; if the host has
        # given one away while the VM was stopped, say so rather than silently
        # moving the SSH endpoint out from under a copied command.
        for label, port in (("SSH", runtime.ssh_port), ("QMP", runtime.qmp_port), ("VNC", runtime.vnc_port)):
            if not is_port_free(port):
                raise ComputeEngineError(
                    f"Cannot start '{name}': its pinned {label} port {port} is "
                    "in use by another process"
                )

        self._spawn(name, runtime)
        if runtime.ssh_enabled:
            elapsed = self._wait_for_ssh(
                name, runtime, self._settings.qemu_boot_timeout_seconds
            )
        else:
            elapsed = self._wait_for_qmp(name, runtime)
        logger.info("Instance '%s' restarted in %.1fs", name, elapsed)

    def clone_disk(self, source: str, target: str, disk_gb: int | None = None) -> None:
        """Flatten a stopped instance's disk into a new instance's directory.

        ``qemu-img convert``, not ``create -b``. An overlay chain would clone in
        milliseconds and cost nothing on disk — and would make deleting the
        source silently corrupt every clone taken from it, because the clone's
        data lives in a file it does not own. This project has refused that kind
        of hidden coupling everywhere else (see DECISIONS #23 on volumes
        outliving instances); a clone that survives its origin being terminated
        is worth the copy.

        The result is a standalone qcow2 with no backing file: the source's
        overlay *and* everything it inherited from the base image, merged.
        """
        source_disk = self._disk_path(source)
        if not source_disk.exists():
            raise ComputeEngineError(f"'{source}' has no disk to clone")

        # The guard that makes this safe. qemu-img reading a disk a live QEMU is
        # writing would copy a filesystem mid-write — the same reasoning as
        # snapshots, and on Windows there is no lock to stop it.
        self._refuse_if_running(source, "clone")

        target_dir = self._dir(target)
        target_dir.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                self._img_binary, "convert",
                "-O", "qcow2",
                str(source_disk),
                str(target_dir / _DISK_FILE),
            ],
            timeout=self._settings.qemu_snapshot_timeout_seconds,
        )
        if disk_gb:
            # A clone inherits the source's virtual size; growing it here keeps
            # "the size I asked for" true. Shrinking is never attempted — it
            # would truncate a filesystem.
            self._run(
                [self._img_binary, "resize", str(target_dir / _DISK_FILE), f"{disk_gb}G"],
                timeout=self._settings.qemu_snapshot_timeout_seconds,
            )
        logger.info("Cloned '%s' disk -> '%s' (flattened)", source, target)

    def boot_cloned_instance(
        self,
        name: str,
        *,
        cpus: int,
        memory: str,
        cloud_init_path: str | None = None,
        options: LaunchOptions | None = None,
    ) -> None:
        """Bring up an instance whose disk already exists.

        The tail of ``provision_instance`` with the disk-creation step removed:
        allocate fresh ports, write a fresh seed, spawn, wait for SSH. Ports and
        seed are *not* copied from the source — two instances sharing a host
        port would collide, and a guest that boots believing it is the machine
        it was copied from is a support ticket waiting to happen.
        """
        options = options or LaunchOptions()
        # The guest family has to survive the clone. The caller passes it (the
        # clone route has always done so, and says why), but this method used to
        # drop it and let _allocate_runtime default to "linux" — which handed a
        # cloned Windows instance a virtio root disk it has no driver for, and
        # then waited out the boot timeout for an SSH service it will never run.
        # Both of those follow from the profile, so both are derived from it
        # here rather than assumed, exactly as provision_instance does.
        profile = guest_profile(options.guest_os)
        seed_cloud_init = options.seed_cloud_init and profile.supports_cloud_init
        runtime = self._allocate_runtime(
            name,
            cpus=cpus,
            memory=memory,
            accel=self.resolve_accel(options.accel, options.guest_os),
            ssh_enabled=seed_cloud_init,
            display=options.display or "std",
            guest_os=options.guest_os,
        )
        if seed_cloud_init:
            self._write_seed(name, cloud_init_path)
        self._write_runtime(name, runtime)
        self._spawn(name, runtime)
        if not runtime.ssh_enabled:
            # Console-only guest: there is no readiness signal to wait for, and
            # blocking here would report a healthy clone as a timeout.
            logger.info(
                "Clone '%s' booted (%s guest: console only, no SSH wait)",
                name, options.guest_os,
            )
            return
        elapsed = self._wait_for_ssh(name, runtime, self._settings.qemu_boot_timeout_seconds)
        logger.info("Clone '%s' booted in %.1fs", name, elapsed)

    def restart_instance(self, name: str) -> None:
        """Graceful restart: ACPI powerdown, wait for exit, boot again.

        Not QMP ``system_reset``. That is the reset button — it yanks the
        virtual power without telling the guest, so a filesystem with dirty
        pages gets the same treatment a power cut would give it. Reusing
        stop+start means a restart is exactly as safe as the stop path already
        is, including its 90-second grace period and forced kill backstop.
        """
        self.stop_instance(name)
        self.start_instance(name)

    def add_port_forward(self, name: str, spec: str) -> None:
        """Add a host port forward, live if the instance is running.

        Written to the runtime file either way, because that is what a restart
        replays. Applied over QMP as well when there is a process to apply it
        to — unlike a volume, a forward *can* be changed on a running guest
        safely: SLIRP owns the socket, nothing in the guest is holding state
        about it, and QEMU starts listening immediately (DECISIONS #24).
        """
        runtime = self._require_runtime(name)
        if spec in runtime.port_forwards:
            return
        if self._is_running(runtime):
            # Applied before it is persisted: if QEMU refuses — a port taken
            # since we checked — the runtime file must not claim otherwise.
            hostfwd_add(HOST_IP, runtime.qmp_port, NETDEV_ID, spec)
        runtime.port_forwards.append(spec)
        self._write_runtime(name, runtime)
        logger.info("Added port forward %s to '%s'", spec, name)

    def remove_port_forward(self, name: str, spec: str) -> None:
        """Remove a host port forward, live if the instance is running.

        The removal is tolerant in one direction only: a forward QEMU does not
        know about is still dropped from the runtime file, because the goal is
        that it is gone. A forward QEMU refuses to drop raises.
        """
        runtime = self._require_runtime(name)
        if self._is_running(runtime):
            # hostfwd_remove keys on the host side alone.
            host_side = spec.rsplit("-", 1)[0]
            try:
                hostfwd_remove(HOST_IP, runtime.qmp_port, NETDEV_ID, host_side)
            except QmpError as exc:
                if "not found" not in str(exc).lower():
                    raise
                logger.info("QEMU had no forward %s to remove; clearing the record", spec)
        runtime.port_forwards = [s for s in runtime.port_forwards if s != spec]
        self._write_runtime(name, runtime)
        logger.info("Removed port forward %s from '%s'", spec, name)

    def set_volumes(self, name: str, paths: list[str]) -> None:
        """Record the ordered volume paths this instance boots with.

        Called by the router after every attach and detach. Takes effect on the
        next start, which is the whole design: attaching to a running VM is
        refused, so there is never a live device to coordinate with — see
        DECISIONS #22 for the measurements behind that.
        """
        runtime = self._require_runtime(name)
        runtime.volumes = list(paths)
        self._write_runtime(name, runtime)
        logger.info("Instance '%s' now boots with %d volume(s)", name, len(paths))

    def stop_instance(self, name: str) -> None:
        """Graceful ACPI powerdown, with a forced kill as the backstop."""
        runtime = self._require_runtime(name)
        if not pid_alive(runtime.pid):
            logger.info("Instance '%s' is already stopped", name)
            self._clear_pid(name, runtime)
            return

        try:
            system_powerdown(HOST_IP, runtime.qmp_port)
            logger.info("Sent system_powerdown to '%s'; waiting for exit", name)
        except QmpError as exc:
            logger.warning("QMP powerdown of '%s' failed (%s) — killing process", name, exc)

        deadline = time.monotonic() + self._settings.qemu_shutdown_timeout_seconds
        while time.monotonic() < deadline:
            if not pid_alive(runtime.pid):
                logger.info("Instance '%s' powered down cleanly", name)
                self._clear_pid(name, runtime)
                return
            time.sleep(1.0)

        logger.warning(
            "Instance '%s' ignored ACPI powerdown for %ds — terminating",
            name, self._settings.qemu_shutdown_timeout_seconds,
        )
        self._force_off(name, runtime)

    def destroy_instance(self, name: str) -> None:
        """Kill the VM and delete its directory. Idempotent."""
        runtime = self._read_runtime(name)
        if runtime is not None and pid_alive(runtime.pid):
            try:
                quit_vm(HOST_IP, runtime.qmp_port)
            except QmpError as exc:
                logger.warning("QMP quit of '%s' failed (%s) — killing process", name, exc)
            self._wait_for_exit(runtime, timeout=15.0)
            terminate_pid(runtime.pid)
            self._wait_for_exit(runtime, timeout=10.0)

        self._remove_dir(name)
        logger.info("Destroyed instance '%s'", name)

    def get_instance_info(self, name: str) -> InstanceInfo:
        directory = self._dir(name)
        if not directory.exists():
            return InstanceInfo(name=name, exists=False, status=None, ip_address=None)

        runtime = self._read_runtime(name)
        if runtime is None:
            # Directory without runtime state: a provision that died mid-setup.
            return InstanceInfo(name=name, exists=True, status=InstanceStatus.ERROR, ip_address=None)

        running = self._is_running(runtime)
        # A guest with no injected key has no SSH to advertise — true for ISO
        # installs and for images without cloud-init. Claiming an endpoint for
        # them would send users to a port nothing is listening on.
        #
        # ``iso_path`` is checked too, not just ``ssh_enabled``: runtime files
        # written before that field existed default it to True, and an ISO VM
        # already on disk must not start advertising SSH after an upgrade.
        has_key = runtime.ssh_enabled and not runtime.iso_path
        # An address is a promise that the guest *answers* there, and it has to
        # be earned by asking. Liveness is not enough: QEMU's process is up and
        # QMP answers within a second of spawn, while the guest needs anywhere
        # from twenty seconds to four minutes to start sshd. Reporting an
        # address on liveness alone made every consumer of this field wrong at
        # once — the reconciler promoted rows to Running mid-boot (measured on
        # a TCG host: Running at 30s, actually reachable at 254s), the dashboard
        # showed a green instance that refused connections, and `degraded`
        # could never fire for QEMU at all, because it triggers on a Running
        # instance *without* an address and this engine always supplied one.
        reachable_over_ssh = running and has_key and (
            self._probe_ssh_banner(runtime.ssh_port, timeout=_READY_PROBE_SECONDS)
            is not None
        )
        return InstanceInfo(
            name=name,
            exists=True,
            status=InstanceStatus.RUNNING if running else InstanceStatus.STOPPED,
            # The guest is only reachable through the host's forwarded port.
            ip_address=HOST_IP if reachable_over_ssh else None,
            ssh_port=runtime.ssh_port if has_key else None,
            vnc_port=runtime.vnc_port,
            qmp_port=runtime.qmp_port,
            pid=runtime.pid if running else None,
            accel=runtime.accel,
            display=runtime.display,
            ssh_enabled=has_key,
        )

    def list_instances(self) -> dict[str, InstanceInfo]:
        if not self._instances_dir.exists():
            return {}
        return {
            child.name: self.get_instance_info(child.name)
            for child in self._instances_dir.iterdir()
            if child.is_dir()
        }

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # Snapshots
    # ------------------------------------------------------------------ #
    #: qcow2 internal snapshots, and only while the instance is stopped.
    #:
    #: Live snapshots were assessed against this build and rejected on evidence,
    #: not on effort. Both QMP routes to a full VM-state snapshot —
    #: ``snapshot-save`` and HMP ``savevm`` — fail identically under WHPX:
    #:
    #:     State blocked due to non-migratable CPUID feature support,
    #:     dirty memory tracking support, and XSAVE/XRSTOR support
    #:
    #: which is the same dirty-memory-tracking gap that makes a text-mode guest
    #: render a blank console (see ACCELS_WITHOUT_DIRTY_TRACKING). It is the
    #: accelerator and not the build: the identical command on the identical
    #: binary under TCG saved 979 KiB of VM state without complaint.
    #:
    #: ``blockdev-snapshot-internal-sync`` *does* work on a running guest, and
    #: that is the trap: it reports success and writes a snapshot whose VM state
    #: is zero bytes. Restoring one gives a disk as it stood mid-write, with the
    #: guest's page cache never flushed — a power-cut, offered under the name
    #: "snapshot". Refusing with a clear 409 is the honest option.
    supports_clone = True
    supports_port_forwards = True
    supports_volumes = True
    supports_snapshots = True
    snapshots_require_stopped = True

    def _disk_path(self, name: str) -> Path:
        return self._dir(name) / _DISK_FILE

    def create_snapshot(self, name: str, tag: str) -> SnapshotInfo:
        """Take a qcow2 internal snapshot of the instance's overlay.

        The caller guarantees the instance is stopped; this checks anyway,
        because ``qemu-img`` writing to a disk QEMU has open is how a qcow2
        gets corrupted, and the file lock that would normally prevent it is not
        something to rely on for the one operation whose whole purpose is
        preserving that file.
        """
        disk = self._disk_path(name)
        if not disk.exists():
            raise ComputeEngineError(f"No disk for instance '{name}' at {disk}")
        self._refuse_if_running(name, "snapshot")

        self._run(
            [self._img_binary, "snapshot", "-c", tag, str(disk)],
            timeout=self._settings.qemu_snapshot_timeout_seconds,
        )
        logger.info("Created snapshot '%s' of '%s'", tag, name)

        for snapshot in self.list_snapshots(name):
            if snapshot.tag == tag:
                return snapshot
        # qemu-img exited 0, so it exists; report what we know rather than fail.
        return SnapshotInfo(tag=tag)

    def list_snapshots(self, name: str) -> list[SnapshotInfo]:
        """Snapshots held in the overlay, newest last (qcow2 ordering)."""
        disk = self._disk_path(name)
        if not disk.exists():
            return []

        proc = self._run(
            [self._img_binary, "snapshot", "-l", str(disk)],
            timeout=self._settings.cli_timeout_seconds,
        )
        return _parse_snapshot_list(proc.stdout)

    def restore_snapshot(self, name: str, tag: str) -> None:
        """Roll the overlay back to ``tag``, discarding everything since."""
        disk = self._disk_path(name)
        if not disk.exists():
            raise ComputeEngineError(f"No disk for instance '{name}' at {disk}")
        self._refuse_if_running(name, "restore")

        self._run(
            [self._img_binary, "snapshot", "-a", tag, str(disk)],
            timeout=self._settings.qemu_snapshot_timeout_seconds,
        )
        logger.info("Restored '%s' to snapshot '%s'", name, tag)

    def delete_snapshot(self, name: str, tag: str) -> None:
        """Drop a snapshot from the overlay. Absent is not an error."""
        disk = self._disk_path(name)
        if not disk.exists():
            return
        if tag not in {s.tag for s in self.list_snapshots(name)}:
            logger.info("Snapshot '%s' of '%s' already gone", tag, name)
            return
        # Not merely for consistency: qemu-img cannot open the overlay at all
        # while QEMU holds it ("Failed to get shared write lock"), so a delete
        # attempted on a running instance fails with a lock error that says
        # nothing about instances. Refusing here turns it into a sentence that
        # names the instance and the fix.
        self._refuse_if_running(name, "delete a snapshot of")
        self._run(
            [self._img_binary, "snapshot", "-d", tag, str(disk)],
            timeout=self._settings.qemu_snapshot_timeout_seconds,
        )
        logger.info("Deleted snapshot '%s' of '%s'", tag, name)

    def _refuse_if_running(self, name: str, action: str) -> None:
        """Refuse any snapshot operation on a live instance.

        This guard is the only thing standing between a user and a corrupt
        disk, which is worth stating because the obvious assumption is that
        qemu-img would refuse by itself. Measured on this Windows host, with a
        VM running and QEMU holding the overlay open:

            qemu-img info      -> succeeded
            qemu-img snapshot -c -> succeeded, and wrote the snapshot

        No lock, no warning, two writers on one qcow2. On Linux QEMU takes an
        OFD lock and qemu-img declines with "Failed to get shared write lock";
        on Windows that protection is simply absent, so ours has to be real
        rather than a second opinion.

        Even where the lock does apply, the integrity argument stands on its
        own: a snapshot taken under a running guest captures a filesystem
        mid-write, and restoring one under a live QEMU would swap the disk out
        from beneath its cached metadata.
        """
        runtime = self._read_runtime(name)
        if runtime is not None and self._is_running(runtime):
            raise ComputeEngineError(
                f"Cannot {action} '{name}' while it is running: QEMU has the "
                "disk open, and this accelerator cannot save VM state. Stop "
                "the instance first."
            )

    def _write_seed(self, name: str, cloud_init_path: str | None) -> None:
        """Build the NoCloud seed ISO from the orchestrator's cloud-config.

        When the caller already rendered the document (the normal path — the
        provisioning job writes it once, engine-agnostically) it is used
        verbatim rather than re-rendered here.
        """
        from app.cloud_init import CloudInitError, render_user_data

        if cloud_init_path:
            try:
                user_data = Path(cloud_init_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise SeedIsoError(f"Could not read cloud-init file {cloud_init_path}: {exc}") from exc
        else:
            try:
                user_data = render_user_data(name, self._settings)
            except CloudInitError as exc:
                raise SeedIsoError(str(exc)) from exc

        build_seed_iso(
            self._dir(name) / _SEED_FILE,
            user_data=user_data,
            meta_data=build_meta_data(name),
            network_config=self._build_network_config(),
        )

    def _build_network_config(self) -> str | None:
        """Netplan-style NoCloud network config, or None to leave it to DHCP.

        Works around a real defect in QEMU's user-mode networking on Windows:
        SLIRP hands the guest its own built-in resolver at 10.0.2.3 and proxies
        queries to the host's DNS, but that proxy answers NXDOMAIN for
        everything here — so the guest has working NAT (raw IPs are reachable)
        and no name resolution, which silently breaks cloud-init's package
        installs. Pointing the guest at real resolvers and telling it to ignore
        the DHCP-supplied one fixes name resolution without touching NAT.

        Set ``qemu_guest_nameservers`` to an empty list to keep SLIRP's DNS.
        """
        nameservers = list(self._settings.qemu_guest_nameservers)
        if not nameservers:
            return None
        config = {
            "version": 2,
            "ethernets": {
                # The virtio NIC comes up as enp0s2 on q35; match by prefix so a
                # machine-type change doesn't silently drop the config.
                "default": {
                    "match": {"name": "en*"},
                    "dhcp4": True,
                    # Keep DHCP's address and route, discard its nameserver.
                    "dhcp4-overrides": {"use-dns": False},
                    "nameservers": {"addresses": nameservers},
                }
            },
        }
        return yaml.safe_dump(config, default_flow_style=False, sort_keys=False)

    def _allocate_runtime(
        self,
        name: str,
        *,
        cpus: int,
        memory: str,
        accel: str | None = None,
        iso_path: str | None = None,
        ssh_enabled: bool = True,
        display: str = "std",
        guest_os: str = "linux",
    ) -> InstanceRuntime:
        s = self._settings
        reserved = self._reserved_ports(exclude=name)
        ssh_port = allocate_port(s.qemu_ssh_port_min, s.qemu_ssh_port_max, reserved=reserved)
        reserved.add(ssh_port)
        qmp_port = allocate_port(s.qemu_qmp_port_min, s.qemu_qmp_port_max, reserved=reserved)
        reserved.add(qmp_port)
        vnc_port = allocate_port(s.qemu_vnc_port_min, s.qemu_vnc_port_max, reserved=reserved)
        return InstanceRuntime(
            ssh_port=ssh_port,
            qmp_port=qmp_port,
            vnc_port=vnc_port,
            cpus=cpus,
            memory=memory,
            accel=accel or self.accel(),
            iso_path=iso_path,
            ssh_enabled=ssh_enabled,
            display=guest_profile(guest_os).display_or_default(display),
            guest_os=guest_os,
        )

    def _require_runtime(self, name: str) -> InstanceRuntime:
        runtime = self._read_runtime(name)
        if runtime is None:
            raise ComputeEngineError(
                f"No QEMU runtime state for '{name}' — the instance directory is "
                "missing or incomplete"
            )
        return runtime

    def _spawn(self, name: str, runtime: InstanceRuntime) -> None:
        cmd = self.build_launch_command(name, runtime)
        logger.info("QEMU command for '%s': %s", name, " ".join(cmd))
        try:
            runtime.pid = spawn_detached(cmd, log_path=self._dir(name) / _PROCESS_LOG)
        except ProcessError as exc:
            raise HypervisorUnavailableError(str(exc)) from exc
        self._write_runtime(name, runtime)

    def _liveness(self, runtime: InstanceRuntime) -> str:
        """``"stopped"`` | ``"running"`` | ``"unreachable"``.

        The pid and the QMP socket answer different questions, and collapsing
        them into one boolean produced a real and very confusing failure.

        QEMU's QMP chardev serves **one client at a time**. While anything else
        holds that socket — a debugging session, a future console feature, a
        second backend process — ``is_responsive`` cannot complete a handshake,
        the old two-factor check returned False, and the reconciler concluded
        the instance had stopped. It then rewrote the row to Stopped and cleared
        the pid, for a VM whose process was alive and healthy the whole time.
        Measured: a 40-minute Running/Stopped flap at roughly one flip per
        reconcile, which ended the instant the competing client disconnected.

        The QMP check was there for a reason, though — it guards against a
        *recycled* pid, where the number we stored now belongs to some unrelated
        process and ``pid_alive`` is meaningless. That case and a busy monitor
        look identical through ``is_responsive``, but they are easy to tell
        apart one level down: QEMU holds its QMP port for as long as it lives,
        so a busy monitor still has something bound to it, while a dead QEMU has
        released it. Binding is therefore the tie-breaker, and both concerns are
        served rather than traded off.
        """
        if not pid_alive(runtime.pid):
            return "stopped"
        if is_responsive(HOST_IP, runtime.qmp_port):
            return "running"
        if is_port_free(runtime.qmp_port, HOST_IP):
            # Nothing is listening at all: QEMU is gone and this pid is someone
            # else's. The original two-factor check existed for exactly this.
            return "stopped"
        return "unreachable"

    def _is_running(self, runtime: InstanceRuntime) -> bool:
        """Whether the VM's process is up. Unreachable-over-QMP still counts.

        Deliberately conservative in both directions it matters: the reconciler
        will not demote a live VM, and ``_refuse_if_running`` will not let a
        clone or snapshot read a disk that a live QEMU may be writing just
        because its monitor was busy.
        """
        state = self._liveness(runtime)
        if state == "unreachable":
            logger.warning(
                "QEMU pid %s is alive but QMP on port %s did not answer — "
                "treating as running. QMP serves one client at a time, so "
                "another connection to it will produce this.",
                runtime.pid, runtime.qmp_port,
            )
        return state != "stopped"

    def _wait_for_ssh(self, name: str, runtime: InstanceRuntime, timeout: int) -> float:
        """Block until the guest's forwarded SSH port accepts a connection.

        A ``hostfwd`` port is *always* accepted by QEMU's SLIRP stack, connected
        or not — but if the guest isn't listening yet the connection is closed
        immediately without a banner. Reading the SSH banner is therefore the
        only honest "the guest is up" signal.
        """
        start = time.monotonic()
        deadline = start + timeout
        last_error = "no connection attempt completed"
        while time.monotonic() < deadline:
            if not pid_alive(runtime.pid):
                raise ComputeEngineError(
                    f"QEMU process for '{name}' exited during boot — see "
                    f"{self._dir(name) / _PROCESS_LOG}"
                )
            banner = self._probe_ssh_banner(runtime.ssh_port)
            if banner:
                logger.info("SSH banner from '%s': %s", name, banner)
                return time.monotonic() - start
            last_error = "guest SSH port not answering yet"
            time.sleep(3.0)

        raise ComputeTimeoutError(
            f"'{name}' did not become SSH-reachable within {timeout}s "
            f"({last_error}); serial console: {self._dir(name) / _SERIAL_LOG}"
        )

    def _wait_for_qmp(self, name: str, runtime: InstanceRuntime, timeout: int = 60) -> float:
        """Block until QEMU's control socket answers.

        The readiness signal for guests we cannot log into. An ISO boots to
        whatever its author chose — an installer, a live shell, a menu — and no
        network service is guaranteed, so "the hypervisor is running this VM" is
        the strongest claim we can honestly make. The user takes it from the
        console.
        """
        start = time.monotonic()
        deadline = start + timeout
        while time.monotonic() < deadline:
            if not pid_alive(runtime.pid):
                raise ComputeEngineError(
                    f"QEMU process for '{name}' exited during boot — see "
                    f"{self._dir(name) / _PROCESS_LOG}"
                )
            if is_responsive(HOST_IP, runtime.qmp_port):
                return time.monotonic() - start
            time.sleep(1.0)
        raise ComputeTimeoutError(
            f"'{name}' did not answer QMP within {timeout}s; "
            f"see {self._dir(name) / _PROCESS_LOG}"
        )

    @staticmethod
    def _probe_ssh_banner(port: int, timeout: float = 5.0) -> str | None:
        """Return the SSH identification string, or None if not listening yet."""
        try:
            with socket.create_connection((HOST_IP, port), timeout) as sock:
                sock.settimeout(timeout)
                data = sock.recv(256)
        except OSError:
            return None
        text = data.decode("utf-8", errors="replace").strip()
        return text if text.startswith("SSH-") else None

    def _wait_for_exit(self, runtime: InstanceRuntime, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not pid_alive(runtime.pid):
                return True
            time.sleep(0.5)
        return not pid_alive(runtime.pid)

    def _force_off(self, name: str, runtime: InstanceRuntime) -> None:
        terminate_pid(runtime.pid)
        self._wait_for_exit(runtime, timeout=10.0)
        self._clear_pid(name, runtime)

    def _clear_pid(self, name: str, runtime: InstanceRuntime) -> None:
        runtime.pid = None
        if self._dir(name).exists():
            self._write_runtime(name, runtime)

    def _remove_dir(self, name: str) -> None:
        """Delete an instance directory, tolerating Windows' lazy file handles."""
        directory = self._dir(name)
        if not directory.exists():
            return
        for attempt in range(5):
            try:
                shutil.rmtree(directory)
                return
            except OSError as exc:
                # QEMU can still hold disk.qcow2 open for a beat after exiting.
                logger.debug("rmtree of %s failed (attempt %d): %s", directory, attempt + 1, exc)
                time.sleep(1.0)
        raise ComputeEngineError(
            f"Could not remove instance directory {directory} — a process may "
            "still be holding its disk open"
        )
