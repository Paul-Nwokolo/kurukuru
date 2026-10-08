"""Linux host facts, exercised on any OS through injected lookups.

These decide what ``doctor`` tells a Linux user whose VMs will not start, so
each branch is a message someone will act on. The KVM-group trap in particular
— added to the group, still denied, because group membership is fixed at login
— is the one a user cannot diagnose from the error QEMU prints.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kurukuru import linux_host
from kurukuru.linux_host import Distro, kvm_access, qemu_install_hint, read_distro

KVM_GID = 108


def _stat(mode: int = stat.S_IFCHR | 0o660, gid: int = KVM_GID):
    return lambda _path: SimpleNamespace(st_mode=mode, st_gid=gid)


def _groups(members: list[str]):
    return lambda gid: SimpleNamespace(gr_name="kvm", gr_mem=members)


def _absent(_path):
    raise FileNotFoundError(_path)


# --------------------------------------------------------------------------- #
# /dev/kvm
# --------------------------------------------------------------------------- #
def test_no_device_means_no_virtualization_here():
    """WSL2 and most cloud VMs: the device is simply not there."""
    assert kvm_access(stat_fn=_absent, user="ada").state == "absent"


def test_an_openable_device_is_ok():
    access = kvm_access(stat_fn=_stat(), can_open=lambda p: True, user="ada",
                        group_lookup=_groups([]), process_gids=[])
    assert access.state == "ok"
    assert access.group == "kvm"


def test_a_user_outside_the_group_is_told_so():
    access = kvm_access(stat_fn=_stat(), can_open=lambda p: False, user="ada",
                        group_lookup=_groups(["someone-else"]), process_gids=[1000])
    assert access.state == "not-in-group"
    assert (access.group, access.user) == ("kvm", "ada")


def test_added_to_the_group_but_not_yet_logged_in_again():
    """The trap. ``usermod -aG kvm`` edits /etc/group; this process was started
    before that and does not carry the group, so access is still denied. The
    remedy is a new login, not running usermod a second time."""
    access = kvm_access(stat_fn=_stat(), can_open=lambda p: False, user="ada",
                        group_lookup=_groups(["ada"]), process_gids=[1000])
    assert access.state == "relogin-needed"


def test_denied_for_a_reason_the_group_does_not_explain():
    """Already in the group in this very process, or the mode grants the group
    nothing: something else — an ACL, a container — is refusing."""
    in_group = kvm_access(stat_fn=_stat(), can_open=lambda p: False, user="ada",
                          group_lookup=_groups(["ada"]), process_gids=[KVM_GID])
    owner_only = kvm_access(stat_fn=_stat(stat.S_IFCHR | 0o600), can_open=lambda p: False,
                            user="ada", group_lookup=_groups([]), process_gids=[])
    assert in_group.state == "denied"
    assert owner_only.state == "denied"


# --------------------------------------------------------------------------- #
# Distribution and the QEMU package
# --------------------------------------------------------------------------- #
def _os_release(tmp_path: Path, text: str) -> str:
    path = tmp_path / "os-release"
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("os_release", "expected"),
    [
        ('ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\n', "apt install qemu-system-x86 qemu-utils"),
        ('ID=linuxmint\nID_LIKE="ubuntu debian"\n', "apt install qemu-system-x86 qemu-utils"),
        ("ID=fedora\n", "dnf install qemu-system-x86-core qemu-img"),
        ("ID=arch\n", "pacman -S qemu-base"),
        ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', "zypper install qemu-x86 qemu-tools"),
    ],
)
def test_the_install_command_matches_the_distribution(tmp_path, os_release, expected):
    assert expected in qemu_install_hint(read_distro(_os_release(tmp_path, os_release)))


def test_rhel_is_told_the_truth_rather_than_a_command_that_does_not_help(tmp_path):
    """qemu-kvm on RHEL installs /usr/libexec/qemu-kvm and no
    qemu-system-x86_64, so "dnf install qemu-kvm" would install something this
    product then reports as missing."""
    hint = qemu_install_hint(read_distro(_os_release(tmp_path, 'ID="rocky"\nID_LIKE="rhel centos fedora"\n')))
    assert "/usr/libexec/qemu-kvm" in hint
    assert not hint.startswith("sudo")


def test_an_unknown_distribution_gets_the_binary_names(tmp_path):
    hint = qemu_install_hint(read_distro(_os_release(tmp_path, "ID=gentoo\n")))
    assert "qemu-system-x86_64" in hint and "qemu-img" in hint


def test_no_os_release_is_not_an_error(tmp_path):
    assert read_distro(str(tmp_path / "missing")) == Distro()


# --------------------------------------------------------------------------- #
# Lingering and the user manager
# --------------------------------------------------------------------------- #
def test_linger_is_read_from_logind_s_marker(tmp_path):
    (tmp_path / "ada").write_text("")
    assert linux_host.linger_enabled("ada", str(tmp_path)) is True
    assert linux_host.linger_enabled("bob", str(tmp_path)) is False


def test_no_logind_means_unknown_not_false(tmp_path):
    """A host without logind cannot linger at all; reporting False would send
    someone to run `loginctl enable-linger` on a machine with no loginctl."""
    assert linux_host.linger_enabled("ada", str(tmp_path / "no-such-dir")) is None


def test_the_user_manager_needs_its_runtime_bus(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_host.shutil, "which", lambda name: "/usr/bin/systemctl")
    (tmp_path / "systemd").mkdir()
    assert linux_host.systemd_user_available({"XDG_RUNTIME_DIR": str(tmp_path)}) is False
    (tmp_path / "systemd" / "private").write_text("")
    assert linux_host.systemd_user_available({"XDG_RUNTIME_DIR": str(tmp_path)}) is True
    assert linux_host.systemd_user_available({}) is False


@pytest.mark.posix
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="probes the real Linux host")
def test_on_a_real_posix_host_describe_answers_without_raising():
    """The defaults, against the real host. Says nothing about what the
    answers are — CI has no /dev/kvm, a laptop does — only that every probe
    copes with what it finds."""
    host = linux_host.describe().as_dict()
    assert set(host) == {"distro", "qemu_install", "kvm", "linger", "systemd_user", "wsl"}
    assert isinstance(host["wsl"], bool)
    assert host["kvm"]["state"] in {"ok", "absent", "not-in-group", "relogin-needed", "denied"}


def test_wsl_is_recognised_by_its_variable_or_its_kernel(tmp_path):
    release = tmp_path / "osrelease"
    release.write_text("6.18.40.1-microsoft-standard-WSL2")
    plain = tmp_path / "plain"
    plain.write_text("6.8.0-45-generic")

    assert linux_host.is_wsl({"WSL_DISTRO_NAME": "Ubuntu-24.04"}, str(plain)) is True
    assert linux_host.is_wsl({}, str(release)) is True
    assert linux_host.is_wsl({}, str(plain)) is False
    assert linux_host.is_wsl({}, str(tmp_path / "missing")) is False
