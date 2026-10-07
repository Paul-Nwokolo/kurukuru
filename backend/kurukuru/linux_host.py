"""
Facts about a Linux host that decide whether Kurukuru can run there.

Facts, not verdicts — the same contract as ``GET /diagnostics``, which carries
these to ``doctor``. Every function takes its inputs (file paths, user and group
lookups) as parameters so the decision logic runs on any platform under test;
the defaults read the real host.

Three questions, each one a way a Linux user meets a failure that looks like a
product fault:

* **Which QEMU package?** QEMU is not bundled on Linux (DECISIONS #70): the
  distribution ships it, patches it and builds it against KVM. So a missing
  binary is answered with the install command for *this* distribution, not a
  generic "install QEMU".
* **Can this process open /dev/kvm?** A user outside the ``kvm`` group gets
  ``Permission denied`` from QEMU, which reads like Kurukuru is broken. And the
  fix has a trap: ``usermod -aG kvm`` changes the *group file*, not any running
  process, so nothing improves until a new login session — and with lingering
  on, the systemd user manager that runs the service survives logout and keeps
  its old groups until it is restarted too.
* **Will the user service outlive the login?** Only with lingering. Without it
  systemd stops the user manager — and the backend with it — when the last
  session ends. Reported as a fact so the docs and ``doctor`` can say it
  plainly instead of implying the service always runs.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path

KVM_DEVICE = "/dev/kvm"
OS_RELEASE = "/etc/os-release"
LINGER_DIR = "/var/lib/systemd/linger"

#: The install command per distribution family, keyed by os-release ``ID`` or an
#: ``ID_LIKE`` entry. Package names were checked against each distribution's
#: package index, but only Ubuntu has actually been installed from (Phase 9);
#: INSTALL-LINUX.md says so.
#:
#: RHEL and its rebuilds are deliberately absent. Their ``qemu-kvm`` package
#: installs ``/usr/libexec/qemu-kvm`` and no ``qemu-system-x86_64`` on PATH at
#: all, so the generic command would install something this product would then
#: report as missing. They get the honest answer instead — see
#: :func:`qemu_install_hint`.
QEMU_PACKAGES: dict[str, str] = {
    "debian": "sudo apt install qemu-system-x86 qemu-utils",
    "ubuntu": "sudo apt install qemu-system-x86 qemu-utils",
    "fedora": "sudo dnf install qemu-system-x86-core qemu-img",
    "arch": "sudo pacman -S qemu-base",
    "opensuse": "sudo zypper install qemu-x86 qemu-tools",
    "suse": "sudo zypper install qemu-x86 qemu-tools",
}

#: Families whose packaged QEMU does not provide ``qemu-system-x86_64``.
NO_UPSTREAM_BINARY = frozenset({"rhel", "centos", "rocky", "almalinux"})


@dataclass(frozen=True)
class Distro:
    id: str = ""
    id_like: tuple[str, ...] = ()
    pretty_name: str = ""

    @property
    def families(self) -> tuple[str, ...]:
        return (self.id, *self.id_like)


def read_distro(path: str = OS_RELEASE) -> Distro:
    """``/etc/os-release``, the one file every systemd-era distribution has."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Distro()
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return Distro(
        id=values.get("ID", "").lower(),
        id_like=tuple(values.get("ID_LIKE", "").lower().split()),
        pretty_name=values.get("PRETTY_NAME", ""),
    )


def qemu_install_hint(distro: Distro) -> str:
    """The command that installs QEMU here, or what to do when there isn't one.

    The RHEL check comes first, deliberately: Rocky and Alma declare
    ``ID_LIKE="rhel centos fedora"``, so a family lookup reaches "fedora" and
    recommends a package that is not in their repositories. Caught by
    ``test_rhel_is_told_the_truth_rather_than_a_command_that_does_not_help``.
    """
    if any(family in NO_UPSTREAM_BINARY for family in distro.families):
        return (
            "This distribution's qemu-kvm package installs /usr/libexec/qemu-kvm "
            "rather than qemu-system-x86_64, which Kurukuru has not been tested "
            "with. Install an upstream qemu-system-x86_64 and qemu-img (or point "
            "KURUKURU_QEMU_SYSTEM_BINARY / KURUKURU_QEMU_IMG_BINARY at them)."
        )
    for family in distro.families:
        if family in QEMU_PACKAGES:
            return QEMU_PACKAGES[family]
    return (
        "Install your distribution's packages providing qemu-system-x86_64 and "
        "qemu-img, and make sure both are on the backend's PATH."
    )


@dataclass(frozen=True)
class KvmAccess:
    """Whether this process can open /dev/kvm, and if not, why.

    ``state`` is one of:

    * ``ok`` — readable and writable now.
    * ``absent`` — no /dev/kvm: virtualization is off in firmware, the KVM
      module is not loaded, or this is a VM without nested virtualization
      (WSL2 among them, usually).
    * ``not-in-group`` — owned by a group this user is not a member of.
    * ``relogin-needed`` — the group file lists the user, but this process was
      started before that and does not carry the group.
    * ``denied`` — none of the above explains it (an ACL, a container).
    """

    state: str
    device: str = KVM_DEVICE
    group: str | None = None
    mode: str | None = None
    user: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def kvm_access(
    device: str = KVM_DEVICE,
    *,
    can_open=None,
    user: str | None = None,
    process_gids=None,
    group_lookup=None,
    stat_fn=None,
) -> KvmAccess:
    """Diagnose /dev/kvm access.

    Every host-touching input is a parameter, so the decision runs on any OS
    under test; ``pwd`` and ``grp`` are imported only when a real lookup is
    needed, because neither exists on Windows.
    """
    stat_fn = stat_fn or os.stat
    try:
        info = stat_fn(device)
    except FileNotFoundError:
        return KvmAccess("absent", device)
    except OSError:
        return KvmAccess("denied", device)

    can_open = can_open or (lambda p: os.access(p, os.R_OK | os.W_OK))
    if user is None:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    if group_lookup is None:
        import grp

        group_lookup = grp.getgrgid
    try:
        group = group_lookup(info.st_gid)
        group_name, members = group.gr_name, set(group.gr_mem)
    except KeyError:
        group_name, members = str(info.st_gid), set()
    mode = stat.filemode(info.st_mode)

    if can_open(device):
        return KvmAccess("ok", device, group_name, mode, user)

    gids = set(process_gids if process_gids is not None else os.getgroups())
    group_grants = bool(info.st_mode & stat.S_IRGRP and info.st_mode & stat.S_IWGRP)
    if group_grants and info.st_gid not in gids:
        state = "relogin-needed" if user in members else "not-in-group"
        return KvmAccess(state, device, group_name, mode, user)
    return KvmAccess("denied", device, group_name, mode, user)


def linger_enabled(user: str | None = None, linger_dir: str = LINGER_DIR) -> bool | None:
    """Whether systemd keeps this user's manager running after logout.

    Read from the marker file ``loginctl enable-linger`` creates, rather than by
    running ``loginctl``: no subprocess, no D-Bus, and the same answer. None
    when this host has no systemd-logind to ask.
    """
    root = Path(linger_dir)
    if not root.is_dir():
        return None
    if user is None:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    return (root / user).exists()


def systemd_user_available(environ=None) -> bool:
    """Whether ``systemctl --user`` has a manager to talk to.

    Needs ``systemctl`` and a per-user runtime directory with the manager's
    bus in it. Absent under a plain container, under WSL without systemd
    enabled, and over an SSH session to a host whose logind never started a
    user manager — each of which makes ``systemctl --user`` fail with a message
    about the bus that says nothing about why.
    """
    environ = os.environ if environ is None else environ
    if shutil.which("systemctl") is None:
        return False
    runtime = environ.get("XDG_RUNTIME_DIR")
    return bool(runtime) and (Path(runtime) / "systemd" / "private").exists()


def is_wsl(environ=None, osrelease: str = "/proc/sys/kernel/osrelease") -> bool:
    """Whether this is Windows Subsystem for Linux.

    It matters for one fact the rest of this module would otherwise get wrong:
    WSL stops the whole distribution when its last window closes, lingering or
    not — measured in Phase 18, with ``Linger=yes`` and the instance still
    ``Stopped`` a minute after the last terminal closed. So under WSL a user
    service runs only while some WSL session is open.
    """
    environ = os.environ if environ is None else environ
    if environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path(osrelease).read_text(encoding="utf-8").lower()
    except OSError:
        return False


@dataclass
class LinuxHost:
    distro: Distro = field(default_factory=Distro)
    qemu_install: str = ""
    kvm: KvmAccess | None = None
    linger: bool | None = None
    systemd_user: bool = False
    wsl: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "distro": asdict(self.distro),
            "qemu_install": self.qemu_install,
            "kvm": self.kvm.as_dict() if self.kvm else None,
            "linger": self.linger,
            "systemd_user": self.systemd_user,
            "wsl": self.wsl,
        }


def describe() -> LinuxHost:
    distro = read_distro()
    return LinuxHost(
        distro=distro,
        qemu_install=qemu_install_hint(distro),
        kvm=kvm_access(),
        linger=linger_enabled(),
        systemd_user=systemd_user_available(),
        wsl=is_wsl(),
    )
