"""
Unit tests for the QEMU engine's pure logic.

Nothing here boots a VM: command construction, port allocation, seed-ISO
contents and state derivation are all testable without QEMU, and that is where
the mistakes that cost an hour of live debugging actually live. The live
end-to-end run is the separate, manual proof that these pieces compose.

``qemu_dir`` is redirected to a tmp_path in every test, so no test can touch the
real ``~/.local-iaas/qemu`` tree.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import app.engines.qemu as qemu_module
from app.config import Settings
from app.engines.base import (
    ComputeEngineError,
    ComputeTimeoutError,
    HypervisorUnavailableError,
    LaunchOptions,
)
from app.engines.ports import PortAllocationError, allocate_port, is_port_free
from app.engines.qemu import (
    HOST_IP,
    WINDOWS_WHPX_CPU,
    InstanceRuntime,
    QemuEngine,
    guest_profile,
)
from app.engines.seed import VOLUME_LABEL, build_meta_data, build_seed_iso, read_seed_file
from app.models import InstanceStatus


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(qemu_dir=str(tmp_path / "qemu"))


@pytest.fixture()
def eng(settings: Settings) -> QemuEngine:
    engine = QemuEngine(settings)
    engine._accel = "whpx"  # skip the live probe; acceleration is tested apart
    return engine


def _runtime(**overrides) -> InstanceRuntime:
    base = {"ssh_port": 2200, "qmp_port": 4400, "vnc_port": 5901, "cpus": 2,
            "memory": "2G", "accel": "whpx"}
    return InstanceRuntime(**{**base, **overrides})


# --------------------------------------------------------------------------- #
# Overlay disk command
# --------------------------------------------------------------------------- #
def test_overlay_command_is_a_backed_qcow2(eng, settings):
    cmd = eng.build_overlay_command("web", "5G")

    assert cmd[0] == settings.qemu_img_binary
    assert cmd[1] == "create"
    # -F declares the *backing* file's format; omitting it makes modern qemu-img
    # refuse (or, worse, probe) — which is exactly the bug this asserts against.
    assert cmd[2:4] == ["-f", "qcow2"]
    assert cmd[4:6] == ["-F", "qcow2"]
    assert cmd[6] == "-b"
    assert cmd[7].endswith(settings.qemu_base_image_name)
    assert cmd[8].endswith("disk.qcow2") and "web" in cmd[8]
    assert cmd[9] == "5G"  # size comes last, from the flavor


# --------------------------------------------------------------------------- #
# Launch command
# --------------------------------------------------------------------------- #
def test_launch_command_wires_ports_and_devices(eng, settings):
    cmd = eng.build_launch_command("web", _runtime())

    assert cmd[0] == settings.qemu_system_binary

    def value_after(flag: str) -> str:
        return cmd[cmd.index(flag) + 1]

    assert value_after("-name") == "web"
    assert value_after("-machine") == "q35"
    assert value_after("-smp") == "2"
    assert value_after("-m") == "2G"
    # SSH forward must be loopback-only: a VM port on 0.0.0.0 would expose the
    # guest to the whole LAN.
    assert value_after("-netdev") == f"user,id=n0,hostfwd=tcp:{HOST_IP}:2200-:22"
    assert value_after("-device") == "virtio-net-pci,netdev=n0"
    assert value_after("-qmp") == f"tcp:{HOST_IP}:4400,server,nowait"
    # -vnc takes a *display* number, not a port: 5901 -> :1
    assert value_after("-vnc") == f"{HOST_IP}:1"
    assert value_after("-display") == "none"
    assert value_after("-serial").startswith("file:")

    drives = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]
    assert any(d.endswith("if=virtio,format=qcow2") and "disk.qcow2" in d for d in drives)
    assert any("seed.iso" in d and "media=cdrom" in d for d in drives)


def test_vnc_and_qmp_are_bound_to_loopback_only(eng):
    """Security guarantee behind the web console.

    The console proxy is meant to be the *only* route to a VM's framebuffer. If
    -vnc ever bound 0.0.0.0, every VM's screen — already logged in, no password
    on the RFB socket — would be served to the whole LAN. Same for QMP, which is
    full control of the hypervisor process.
    """
    cmd = eng.build_launch_command("web", _runtime())
    vnc = cmd[cmd.index("-vnc") + 1]
    qmp = cmd[cmd.index("-qmp") + 1]
    hostfwd = cmd[cmd.index("-netdev") + 1]

    assert vnc.startswith("127.0.0.1:")
    assert qmp.startswith("tcp:127.0.0.1:")
    assert "hostfwd=tcp:127.0.0.1:" in hostfwd
    for arg in cmd:
        assert "0.0.0.0" not in arg
        assert "::" not in arg  # no accidental IPv6 wildcard either


def test_launch_command_uses_whpx_when_accelerated(eng):
    cmd = eng.build_launch_command("web", _runtime())
    assert cmd[cmd.index("-accel") + 1] == "whpx,kernel-irqchip=off"


def test_launch_command_falls_back_to_tcg(settings):
    engine = QemuEngine(settings)
    engine._accel = "tcg"
    cmd = engine.build_launch_command("web", _runtime(accel="tcg"))
    # kernel-irqchip=off is a WHPX requirement and must not leak into TCG.
    assert cmd[cmd.index("-accel") + 1] == "tcg"


def test_cpu_model_is_whpx_safe_when_accelerated(eng):
    """WHPX dies with "Unexpected VP exit code 4" on -cpu max/host, before the
    guest writes a single serial line — so accelerated boots use qemu64."""
    cmd = eng.build_launch_command("web", _runtime())
    assert eng.cpu_model() == "qemu64"
    assert cmd[cmd.index("-cpu") + 1] == "qemu64"


def test_cpu_model_is_max_under_tcg(settings):
    engine = QemuEngine(settings)
    engine._accel = "tcg"
    assert engine.cpu_model() == "max"


def test_cpu_model_can_be_overridden_by_settings(tmp_path: Path):
    engine = QemuEngine(Settings(qemu_dir=str(tmp_path), qemu_cpu_model="Skylake-Client"))
    engine._accel = "whpx"
    assert engine.cpu_model() == "Skylake-Client"


# --------------------------------------------------------------------------- #
# ISO boot
# --------------------------------------------------------------------------- #
def test_blank_disk_command_has_no_backing_file(eng, settings):
    """An installer must write to empty space, not on top of someone's rootfs."""
    cmd = eng.build_blank_disk_command("inst", "10G")

    assert cmd[:4] == [settings.qemu_img_binary, "create", "-f", "qcow2"]
    assert "-b" not in cmd and "-F" not in cmd
    assert cmd[-1] == "10G"
    assert cmd[-2].endswith("disk.qcow2")


def test_iso_launch_attaches_media_and_boots_cd_first(eng):
    cmd = eng.build_launch_command("inst", _runtime(iso_path="C:\\isos\\alpine.iso"))

    drives = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]
    assert any("alpine.iso" in d and "media=cdrom" in d for d in drives)
    # order=dc: CD first so the installer runs, disk second so the installed
    # system takes over once the CD is removed.
    assert cmd[cmd.index("-boot") + 1] == "order=dc,menu=on"


def test_iso_launch_carries_no_cloud_init_seed(eng):
    """A generic ISO ignores NoCloud; a second CD-ROM would only confuse boot."""
    cmd = eng.build_launch_command("inst", _runtime(iso_path="C:\\isos\\alpine.iso"))
    drives = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]
    assert not any("seed.iso" in d for d in drives)


# --------------------------------------------------------------------------- #
# Guest OS drives the hardware (Phase 13)
# --------------------------------------------------------------------------- #
def _devices(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-device"]


def _drives(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]


def test_a_linux_guest_is_unchanged_by_the_windows_work(eng):
    """The regression that would matter most: Windows support must not have
    quietly re-specified every existing VM's hardware."""
    cmd = eng.build_launch_command("web", _runtime())

    assert any("if=virtio" in d for d in _drives(cmd))
    assert any(d.startswith("virtio-net-pci,") for d in _devices(cmd))
    assert "ich9-ahci,id=ahci" not in _devices(cmd)
    assert eng.cpu_model("whpx", "linux") == "qemu64"


def test_a_windows_guest_gets_hardware_setup_has_drivers_for(eng):
    """The three inbox devices, together. Windows Setup finds no disk on
    virtio-blk and no network on virtio-net, and reports neither as a driver
    problem — it just shows an empty list."""
    cmd = eng.build_launch_command("win", _runtime(guest_os="windows"))

    devices = _devices(cmd)
    assert "ich9-ahci,id=ahci" in devices
    assert "ide-hd,bus=ahci.0,drive=hd0" in devices
    assert any(d.startswith("e1000e,") for d in devices)
    assert not any("virtio" in d for d in devices)
    assert not any("if=virtio" in d for d in _drives(cmd))


def test_the_ahci_controller_precedes_every_disk_that_names_it(eng):
    """A ``bus=ahci.N`` reference has to resolve to a controller QEMU has
    already seen, so argument order is load-bearing, not cosmetic."""
    cmd = eng.build_launch_command(
        "win", _runtime(guest_os="windows", volumes=["C:\\v\\data.qcow2"])
    )

    controller = cmd.index("ich9-ahci,id=ahci")
    referencing = [i for i, a in enumerate(cmd) if a.startswith(("ide-hd,", "ide-cd,"))]
    assert referencing, "expected at least one disk on the controller"
    assert controller < min(referencing)


def test_windows_volumes_get_their_own_ahci_ports_in_attach_order(eng):
    cmd = eng.build_launch_command(
        "win",
        _runtime(guest_os="windows", volumes=["C:\\v\\a.qcow2", "C:\\v\\b.qcow2"]),
    )

    devices = _devices(cmd)
    # Root disk on port 0, then volumes in the order the runtime file records.
    assert "ide-hd,bus=ahci.0,drive=hd0" in devices
    assert "ide-hd,bus=ahci.1,drive=hd1" in devices
    assert "ide-hd,bus=ahci.2,drive=hd2" in devices
    drives = _drives(cmd)
    assert any("a.qcow2" in d and "id=hd1" in d for d in drives)
    assert any("b.qcow2" in d and "id=hd2" in d for d in drives)


def test_the_windows_installer_iso_lands_after_the_data_disks(eng):
    """The CD takes the port after the last disk, so adding a volume never
    renumbers a disk the guest has already partitioned."""
    cmd = eng.build_launch_command(
        "win", _runtime(guest_os="windows", volumes=["C:\\v\\a.qcow2"],
                        iso_path="C:\\isos\\server.iso")
    )

    assert "ide-cd,bus=ahci.2,drive=cd0" in _devices(cmd)
    assert cmd[cmd.index("-boot") + 1] == "order=dc,menu=on"


def test_windows_gets_a_cpu_with_sse42(eng):
    """Windows 11 and Server 2025 refuse to run without SSE4.2/POPCNT, which
    qemu64 does not carry — so the Phase 5 "WHPX means qemu64" rule had to
    narrow to "WHPX and Linux means qemu64"."""
    assert eng.cpu_model("whpx", "windows") == WINDOWS_WHPX_CPU
    assert eng.cpu_model("whpx", "linux") == "qemu64"
    # The accelerator still has the final say where it must: host-derived
    # models are what break WHPX, and neither guest gets one.
    assert eng.cpu_model("whpx", "windows") not in ("max", "host")


def test_windows_is_never_given_virtio_gpu(eng):
    """Windows Setup has no virtio-gpu driver, so 'modern' graphics is not a
    slower choice there, it is a blank screen. Requesting it is ignored rather
    than honoured into an unusable console."""
    cmd = eng.build_launch_command(
        "win", _runtime(guest_os="windows", display="virtio")
    )
    assert cmd[cmd.index("-vga") + 1] == "std"
    # Linux may still ask for it.
    linux = eng.build_launch_command("web", _runtime(display="virtio"))
    assert linux[linux.index("-vga") + 1] == "virtio"


def test_an_unknown_guest_os_falls_back_to_linux_hardware(eng):
    """Runtime files predate this field. Reading one written before Windows
    existed must produce exactly the hardware that VM was built with."""
    assert guest_profile(None).disk_bus == "virtio"
    assert guest_profile("plan9").disk_bus == "virtio"


def test_no_seed_is_attached_when_none_was_generated(eng):
    """QEMU refuses to start if told to open a CD-ROM file that isn't there.

    An image without cloud-init gets no seed, so the launch args must not name
    one — otherwise the VM dies instantly with "Could not open seed.iso".
    """
    cmd = eng.build_launch_command("plain", _runtime(ssh_enabled=False))
    drives = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]

    assert not any("seed.iso" in d for d in drives)
    assert any("disk.qcow2" in d for d in drives)


def test_seed_is_attached_for_cloud_images(eng):
    cmd = eng.build_launch_command("cloud", _runtime(ssh_enabled=True))
    drives = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-drive"]
    assert any("seed.iso" in d and "media=cdrom" in d for d in drives)


def test_image_launch_has_no_boot_override(eng):
    """Cloud images boot from disk; forcing an order would be noise."""
    cmd = eng.build_launch_command("inst", _runtime())
    assert "-boot" not in cmd


def test_iso_instance_advertises_no_ssh_endpoint(eng):
    """No key injection means no SSH — the row must not imply otherwise."""
    eng._write_runtime("inst", _runtime(pid=99, iso_path="C:\\isos\\alpine.iso"))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
    ):
        info = eng.get_instance_info("inst")

    assert info.status is InstanceStatus.RUNNING
    assert info.ip_address is None
    assert info.ssh_port is None
    assert info.vnc_port == 5901  # the console still works — that's the way in


def test_image_without_cloud_init_advertises_no_ssh_either(eng):
    """Same rule, different cause: no key injected means no endpoint to offer."""
    eng._write_runtime("plain", _runtime(pid=99, ssh_enabled=False))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
    ):
        info = eng.get_instance_info("plain")

    assert info.status is InstanceStatus.RUNNING
    assert info.ip_address is None and info.ssh_port is None


def test_legacy_iso_runtime_file_still_hides_ssh(eng):
    """runtime.json written before ssh_enabled existed defaults it to True; an
    ISO VM already on disk must not start advertising SSH after an upgrade."""
    eng._write_runtime(
        "legacy", _runtime(pid=99, iso_path=r"C:\isos\alpine.iso", ssh_enabled=True)
    )
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
    ):
        info = eng.get_instance_info("legacy")

    assert info.ip_address is None and info.ssh_port is None


def test_iso_path_survives_a_stop_start_cycle(eng):
    """Restarting must re-attach the same media, not boot a blank disk."""
    eng._write_runtime("inst", _runtime(iso_path="C:\\isos\\alpine.iso"))
    assert eng._read_runtime("inst").iso_path == "C:\\isos\\alpine.iso"


# --------------------------------------------------------------------------- #
# Accelerator policy — speed vs. a console that can actually render
# --------------------------------------------------------------------------- #
def test_display_defaults_to_standard_graphics(eng):
    """std VGA needs no guest driver, so *something* renders for any OS —
    which is the whole point of arbitrary-guest ISO boot."""
    cmd = eng.build_launch_command("web", _runtime())
    assert cmd[cmd.index("-vga") + 1] == "std"


def test_display_can_be_switched_to_virtio(eng):
    """The -vga virtio form: one device, virtio-gpu plus VGA compatibility."""
    cmd = eng.build_launch_command("web", _runtime(display="virtio"))
    assert cmd[cmd.index("-vga") + 1] == "virtio"
    # Exactly one display adapter — never both.
    assert cmd.count("-vga") == 1


def test_display_survives_a_stop_start_cycle(eng):
    """Handing a restarted guest different hardware than it bound drivers to
    would break the console it was launched for."""
    eng._write_runtime("web", _runtime(display="virtio"))
    assert eng._read_runtime("web").display == "virtio"


def test_legacy_runtime_file_defaults_to_std(eng):
    """runtime.json written before the field existed ran std VGA."""
    import json
    path = eng._dir("old") / "runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ssh_port": 2200, "qmp_port": 4400, "vnc_port": 5900}))
    assert eng._read_runtime("old").display == "std"


def test_info_reports_the_display(eng):
    eng._write_runtime("web", _runtime(pid=1, display="virtio"))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
    ):
        assert eng.get_instance_info("web").display == "virtio"


def test_hardware_acceleration_is_the_default_for_every_boot_mode(eng):
    """The forced-TCG rule for ISO instances is gone.

    It rested on "accelerated VMs can't render a console", which turned out to
    apply only to guests sitting in VGA text mode — and an ISO installer is
    exactly the kind of guest that switches to a framebuffer. Paying a ~30x boot
    penalty for that was a cost with no benefit.
    """
    assert eng.resolve_accel(None) == "whpx"
    assert eng.resolve_accel("auto") == "whpx"


def test_only_an_explicit_request_selects_software_emulation(eng):
    assert eng.resolve_accel("tcg") == "tcg"
    assert eng.resolve_accel("whpx") == "whpx"


def test_console_caveat_flags_only_text_mode_vga_under_whpx():
    """The real rule, not "WHPX means no console".

    Verified live: an Ubuntu cloud image (never leaves 80x25 text) renders
    nothing on std VGA under WHPX, while the same guest on virtio-gpu and an
    Alpine ISO on std VGA both render live.
    """
    caveat = QemuEngine.console_caveat("whpx", "std")
    assert caveat is not None and "text mode" in caveat

    # Every other combination is expected to work.
    assert QemuEngine.console_caveat("whpx", "virtio") is None
    assert QemuEngine.console_caveat("tcg", "std") is None
    assert QemuEngine.console_caveat("tcg", "virtio") is None


def test_console_caveat_treats_a_missing_display_as_std():
    """Pre-Phase-7 rows recorded no display; they all ran std VGA."""
    assert QemuEngine.console_caveat("whpx", None) is not None


def test_whpx_request_falls_back_when_unavailable(settings):
    engine = QemuEngine(settings)
    engine._accel = "tcg"  # host without Windows Hypervisor Platform
    assert engine.resolve_accel("whpx") == "tcg"


# --------------------------------------------------------------------------- #
# Platform-aware acceleration (Phase 9, Part A)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", "whpx"), ("linux", "kvm"), ("darwin", "hvf"), ("freebsd", None)],
)
def test_the_accelerator_candidate_follows_the_platform(settings, monkeypatch, platform, expected):
    """Each OS has exactly one; probing for another host's is guaranteed to fail.

    The probe used to ask for WHPX unconditionally, so on Linux it always got
    an immediate error and every VM silently fell back to ~30x slower software
    emulation.
    """
    monkeypatch.setattr(qemu_module.sys, "platform", platform)
    assert QemuEngine(settings).native_accel() == expected


@pytest.mark.parametrize(
    ("accel", "expected"),
    [
        # kernel-irqchip=off is mandatory for WHPX and accepted by nothing else.
        ("whpx", "whpx,kernel-irqchip=off"),
        ("kvm", "kvm"),
        ("hvf", "hvf"),
        ("tcg", "tcg"),
        # An unknown name must never reach QEMU as an accelerator.
        ("nonsense", "tcg"),
    ],
)
def test_accel_argument_is_correct_per_backend(accel, expected):
    assert QemuEngine._accel_arg(accel) == expected


@pytest.mark.parametrize(
    ("accel", "expected"),
    [("whpx", "qemu64"), ("kvm", "host"), ("hvf", "host"), ("tcg", "max")],
)
def test_cpu_model_follows_the_accelerator(settings, accel, expected):
    """The WHPX workaround must not hobble guests on other accelerators.

    `qemu64` exists solely because WHPX dies on `max`/`host`. Under KVM, `host`
    is both correct and materially faster — passing the real CPU through gives
    the guest AES-NI and AVX instead of emulated substitutes.
    """
    assert QemuEngine(settings).cpu_model(accel) == expected


def test_a_foreign_accelerator_request_resolves_to_this_host(settings):
    """Asking for WHPX on a KVM host is a request QEMU cannot satisfy.

    Refusing the launch would be worse than honouring the spirit of it: the
    caller wanted hardware acceleration, and this host has some.
    """
    engine = QemuEngine(settings)
    engine._accel = "kvm"
    assert engine.resolve_accel("whpx") == "kvm"
    assert engine.resolve_accel("kvm") == "kvm"
    # An explicit request for software emulation is still honoured exactly.
    assert engine.resolve_accel("tcg") == "tcg"


def test_accelerated_means_hardware_backed_not_whpx(settings, monkeypatch):
    """`/health` and `/host/capacity` report this; it was `== "whpx"`.

    On a KVM host that reported accel_available: false while KVM was working
    perfectly, which is the dashboard telling the user to go fix a non-problem.
    """
    engine = QemuEngine(settings)
    monkeypatch.setattr(engine, "is_available", lambda: True)
    for accel, accelerated in (("whpx", True), ("kvm", True), ("hvf", True), ("tcg", False)):
        engine._accel = accel
        assert engine.describe()["accelerated"] is accelerated


def test_kvm_device_problems_are_reported_with_their_remedy(monkeypatch, tmp_path):
    """"Permission denied on /dev/kvm" has a specific fix; say it.

    Left to QEMU's own message this appears as a one-line stderr the fallback
    warning truncates, and the user concludes their CPU lacks virtualization.
    """
    import app.engines.qemu as module

    missing = tmp_path / "definitely-absent"
    monkeypatch.setattr(module, "Path", lambda p: missing if p == "/dev/kvm" else Path(p))
    reason = QemuEngine._kvm_unavailable_reason()
    assert reason is not None and "does not exist" in reason

    present = tmp_path / "kvm"
    present.write_bytes(b"")
    monkeypatch.setattr(module, "Path", lambda p: present if p == "/dev/kvm" else Path(p))
    monkeypatch.setattr(module.os, "access", lambda path, mode: False)
    reason = QemuEngine._kvm_unavailable_reason()
    assert reason is not None and "kvm' group" in reason


def test_accel_is_taken_from_the_runtime_not_the_host(settings):
    """Two VMs on one host can run under different accelerators."""
    engine = QemuEngine(settings)
    engine._accel = "whpx"
    tcg_cmd = engine.build_launch_command("a", _runtime(accel="tcg"))
    whpx_cmd = engine.build_launch_command("b", _runtime(accel="whpx"))

    assert tcg_cmd[tcg_cmd.index("-accel") + 1] == "tcg"
    assert tcg_cmd[tcg_cmd.index("-cpu") + 1] == "max"
    assert whpx_cmd[whpx_cmd.index("-accel") + 1] == "whpx,kernel-irqchip=off"
    assert whpx_cmd[whpx_cmd.index("-cpu") + 1] == "qemu64"


def test_info_reports_the_accelerator(eng):
    eng._write_runtime("web", _runtime(pid=1234, accel="tcg"))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
    ):
        assert eng.get_instance_info("web").accel == "tcg"


def test_probe_falls_back_to_tcg_when_qemu_exits_immediately(settings, monkeypatch):
    """An accelerator QEMU refuses to start under is not available.

    The platform is pinned so the probe actually reaches QEMU: on Linux the
    /dev/kvm pre-check answers first and QEMU is never run, which is correct
    behaviour and made this assertion about *how* the answer was reached fail
    on the Part B host.
    """
    monkeypatch.setattr(qemu_module.sys, "platform", "win32")
    engine = QemuEngine(settings)
    with patch("app.engines.qemu.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="whpx: Failed to enable partition"
        )
        assert engine.accel() == "tcg"
    assert engine.accel() == "tcg"  # cached, no second probe
    assert run.call_count == 1


@pytest.mark.parametrize("platform, expected", [("win32", "whpx"), ("linux", "kvm"), ("darwin", "hvf")])
def test_probe_reports_the_platform_accelerator_when_qemu_stays_up(
    settings, monkeypatch, platform, expected
):
    """A probe that survives its timeout means the accelerator works.

    Parameterised over the platform rather than asserting "whpx", which is what
    this test used to do — so it passed on the machine it was written on and
    failed on Linux, where the probe correctly reports kvm (or tcg when
    /dev/kvm is missing, as on the Part B host). The mechanism under test is
    the same everywhere: QEMU started with -S sits there, so the timeout *is*
    the success signal.
    """
    monkeypatch.setattr(qemu_module.sys, "platform", platform)
    # The Linux candidate is gated on /dev/kvm; this test is about the probe.
    monkeypatch.setattr(QemuEngine, "_kvm_unavailable_reason", staticmethod(lambda: None))
    engine = QemuEngine(settings)
    with patch(
        "app.engines.qemu.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="qemu", timeout=6),
    ):
        assert engine.accel() == expected


def test_probe_skips_qemu_entirely_when_dev_kvm_is_missing(settings, monkeypatch):
    """No point starting QEMU to be told what /dev/kvm already said.

    This is the Part B host's situation: a cloud VM whose provider exposes no
    nested virtualization. The reason reaches the log in the user's terms
    instead of as a truncated QEMU stderr.
    """
    monkeypatch.setattr(qemu_module.sys, "platform", "linux")
    monkeypatch.setattr(
        QemuEngine, "_kvm_unavailable_reason", staticmethod(lambda: "/dev/kvm does not exist")
    )
    engine = QemuEngine(settings)
    with patch("app.engines.qemu.subprocess.run") as run:
        assert engine.accel() == "tcg"
    run.assert_not_called()


# --------------------------------------------------------------------------- #
# Port allocation
# --------------------------------------------------------------------------- #
def _a_free_port() -> int:
    """Probe the OS for a port that is free *right now*.

    Hardcoding numbers here would make these tests depend on whatever else the
    host happens to be listening on — including a VM this project itself left
    running on 2200.
    """
    import socket

    with socket.socket() as probe:
        probe.bind((HOST_IP, 0))
        return probe.getsockname()[1]


def test_allocate_port_returns_the_lowest_free_port():
    port = _a_free_port()
    assert allocate_port(port, port + 5) == port


def test_allocate_port_skips_reserved():
    port = _a_free_port()
    assert allocate_port(port, port + 5, reserved={port}) != port


def test_allocate_port_skips_a_port_in_use():
    import socket

    with socket.socket() as held:
        held.bind((HOST_IP, 0))
        held.listen(1)
        busy = held.getsockname()[1]
        assert is_port_free(busy) is False
        assert allocate_port(busy, busy + 5) != busy


def test_allocate_port_exhausted_range_raises():
    port = _a_free_port()
    with pytest.raises(PortAllocationError):
        allocate_port(port, port + 2, reserved={port, port + 1, port + 2})


def test_allocate_port_rejects_an_inverted_range():
    with pytest.raises(PortAllocationError):
        allocate_port(2300, 2200)


def test_runtime_allocation_gives_three_distinct_ports_in_range(eng, settings):
    runtime = eng._allocate_runtime("web", cpus=1, memory="1G")

    assert settings.qemu_ssh_port_min <= runtime.ssh_port <= settings.qemu_ssh_port_max
    assert settings.qemu_qmp_port_min <= runtime.qmp_port <= settings.qemu_qmp_port_max
    assert settings.qemu_vnc_port_min <= runtime.vnc_port <= settings.qemu_vnc_port_max
    assert len({runtime.ssh_port, runtime.qmp_port, runtime.vnc_port}) == 3


def test_ports_of_other_instances_are_reserved_even_when_stopped(eng):
    """A stopped VM's ports are unbound but still belong to it."""
    first = eng._allocate_runtime("one", cpus=1, memory="1G")
    eng._write_runtime("one", first)  # 'one' is not running: nothing is bound

    second = eng._allocate_runtime("two", cpus=1, memory="1G")
    assert second.ssh_port != first.ssh_port
    assert second.qmp_port != first.qmp_port
    assert second.vnc_port != first.vnc_port


# --------------------------------------------------------------------------- #
# NoCloud seed ISO
# --------------------------------------------------------------------------- #
def test_seed_iso_round_trips_user_data(tmp_path: Path):
    user_data = "#cloud-config\nusers:\n  - name: iaas\n"
    iso = build_seed_iso(
        tmp_path / "seed.iso", user_data=user_data, meta_data=build_meta_data("web")
    )

    assert iso.exists()
    assert read_seed_file(iso, "user-data") == user_data


def test_seed_iso_meta_data_carries_instance_id_and_hostname(tmp_path: Path):
    iso = build_seed_iso(
        tmp_path / "seed.iso", user_data="#cloud-config\n", meta_data=build_meta_data("web-01")
    )
    meta = read_seed_file(iso, "meta-data")
    assert "instance-id: web-01" in meta
    assert "local-hostname: web-01" in meta


def test_seed_iso_volume_label_is_cidata(tmp_path: Path):
    """NoCloud finds the volume *by label*; get this wrong and no VM has a user."""
    import pycdlib

    iso_path = build_seed_iso(
        tmp_path / "seed.iso", user_data="#cloud-config\n", meta_data=build_meta_data("web")
    )
    iso = pycdlib.PyCdlib()
    iso.open(str(iso_path))
    try:
        label = iso.pvd.volume_identifier.decode("utf-8").strip()
    finally:
        iso.close()
    assert label == VOLUME_LABEL == "CIDATA"


def test_seed_iso_omits_network_config_when_not_supplied(tmp_path: Path):
    from app.engines.seed import SeedIsoError

    iso = build_seed_iso(
        tmp_path / "seed.iso", user_data="#cloud-config\n", meta_data=build_meta_data("web")
    )
    with pytest.raises(SeedIsoError):
        read_seed_file(iso, "network-config")


def test_network_config_overrides_the_dhcp_nameserver(eng):
    """SLIRP's DNS proxy NXDOMAINs everything on Windows, so the guest must be
    told to use real resolvers *instead of* the DHCP-supplied one."""
    import yaml

    config = yaml.safe_load(eng._build_network_config())
    ethernet = config["ethernets"]["default"]

    assert config["version"] == 2
    assert ethernet["dhcp4"] is True  # address/route still come from DHCP
    assert ethernet["dhcp4-overrides"]["use-dns"] is False
    assert ethernet["nameservers"]["addresses"] == ["1.1.1.1", "8.8.8.8"]
    assert ethernet["match"]["name"] == "en*"


def test_network_config_is_omitted_when_nameservers_are_empty(tmp_path: Path):
    engine = QemuEngine(Settings(qemu_dir=str(tmp_path), qemu_guest_nameservers=[]))
    assert engine._build_network_config() is None


def test_engine_seed_carries_the_network_config(eng, tmp_path: Path):
    (eng._dir("web")).mkdir(parents=True)
    rendered = tmp_path / "web.yaml"
    rendered.write_text("#cloud-config\n", encoding="utf-8")
    eng._write_seed("web", str(rendered))

    network = read_seed_file(eng._dir("web") / "seed.iso", "network-config")
    assert "1.1.1.1" in network


def test_engine_seed_matches_the_generated_cloud_init(eng, tmp_path: Path, monkeypatch):
    """The ISO must carry byte-identical YAML to what Multipass would receive."""
    from app import cloud_init

    document = cloud_init.render_user_data(
        "web", Settings(ssh_key_dir=str(tmp_path / "keys"))
    )
    rendered = tmp_path / "web.yaml"
    rendered.write_text(document, encoding="utf-8")

    (eng._dir("web")).mkdir(parents=True)
    eng._write_seed("web", str(rendered))

    assert read_seed_file(eng._dir("web") / "seed.iso", "user-data") == document
    assert document.startswith("#cloud-config\n")
    assert "iaas" in document and "ssh_authorized_keys" in document


# --------------------------------------------------------------------------- #
# State derivation
# --------------------------------------------------------------------------- #
def test_info_missing_directory_means_terminated(eng):
    info = eng.get_instance_info("ghost")
    assert info.exists is False and info.status is None


def test_info_dead_pid_means_stopped_with_ports_retained(eng):
    eng._write_runtime("web", _runtime(pid=None))
    with patch("app.engines.qemu.pid_alive", return_value=False):
        info = eng.get_instance_info("web")

    assert info.exists is True
    assert info.status is InstanceStatus.STOPPED
    assert info.ip_address is None  # nothing to connect to while powered off
    assert info.ssh_port == 2200 and info.vnc_port == 5901 and info.qmp_port == 4400
    assert info.pid is None


def test_info_live_pid_and_responsive_qmp_means_running(eng):
    eng._write_runtime("web", _runtime(pid=1234))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
        patch.object(QemuEngine, "_probe_ssh_banner", return_value="SSH-2.0-x"),
    ):
        info = eng.get_instance_info("web")

    assert info.status is InstanceStatus.RUNNING
    assert info.ip_address == HOST_IP
    assert info.pid == 1234


def test_a_booting_guest_is_running_but_has_no_address_yet(eng):
    """The VM process is up; the guest is not. Those are different claims.

    QMP answers about a second after spawn, while the guest takes anywhere from
    twenty seconds to four minutes to start sshd — measured at 254s on a TCG
    host. Reporting an address on liveness alone let a background reconcile
    pass promote the row to Running mid-boot, so `launch --wait` returned on an
    instance that refused connections for another four minutes.
    """
    eng._write_runtime("web", _runtime(pid=1234))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
        patch.object(QemuEngine, "_probe_ssh_banner", return_value=None),  # still booting
    ):
        info = eng.get_instance_info("web")

    assert info.status is InstanceStatus.RUNNING  # the hypervisor is running it
    assert info.ip_address is None                # but there is nowhere to go yet
    # The port is still reported: it is pinned for the instance's life, and the
    # UI shows where SSH *will* be.
    assert info.ssh_port == 2200


def test_the_readiness_probe_is_skipped_for_guests_with_no_key(eng):
    """An ISO guest never serves SSH, so probing it would only cost a second."""
    eng._write_runtime("inst", _runtime(pid=99, iso_path="/isos/alpine.iso"))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=True),
        patch.object(QemuEngine, "_probe_ssh_banner") as probe,
    ):
        info = eng.get_instance_info("inst")

    probe.assert_not_called()
    assert info.status is InstanceStatus.RUNNING and info.ip_address is None


def test_info_live_pid_but_nothing_bound_to_qmp_is_not_running(eng):
    """Guards against a recycled pid being read as 'the VM is up'.

    A dead QEMU releases its QMP port, so nothing is bound to it. That, rather
    than a failed handshake, is what identifies a stale pid — see _liveness.
    """
    eng._write_runtime("web", _runtime(pid=1234))
    with (
        patch("app.engines.qemu.pid_alive", return_value=True),
        patch("app.engines.qemu.is_responsive", return_value=False),
        patch("app.engines.qemu.is_port_free", return_value=True),
    ):
        assert eng.get_instance_info("web").status is InstanceStatus.STOPPED


def test_info_directory_without_runtime_is_error(eng):
    eng._dir("halfbuilt").mkdir(parents=True)
    info = eng.get_instance_info("halfbuilt")
    assert info.exists is True and info.status is InstanceStatus.ERROR


def test_list_instances_covers_every_directory(eng):
    eng._write_runtime("a", _runtime(ssh_port=2200, qmp_port=4400, vnc_port=5900))
    eng._write_runtime("b", _runtime(ssh_port=2201, qmp_port=4401, vnc_port=5901))
    with patch("app.engines.qemu.pid_alive", return_value=False):
        listing = eng.list_instances()

    assert set(listing) == {"a", "b"}
    assert all(i.status is InstanceStatus.STOPPED for i in listing.values())


def test_list_instances_empty_when_nothing_provisioned(eng):
    assert eng.list_instances() == {}


# --------------------------------------------------------------------------- #
# Runtime persistence
# --------------------------------------------------------------------------- #
def test_runtime_round_trips_through_disk(eng):
    eng._write_runtime("web", _runtime(pid=99, accel="whpx"))
    loaded = eng._read_runtime("web")

    assert loaded == _runtime(pid=99, accel="whpx")
    assert loaded.vnc_display == 1  # 5901 -> :1


def test_unreadable_runtime_file_is_tolerated(eng):
    path = eng._dir("web") / "runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert eng._read_runtime("web") is None


def test_runtime_file_from_a_future_schema_is_ignored(eng):
    path = eng._dir("web") / "runtime.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"ssh_port": 2200, "unexpected": True}), encoding="utf-8")
    assert eng._read_runtime("web") is None


def test_start_without_runtime_state_fails_loudly(eng):
    eng._dir("web").mkdir(parents=True)
    with pytest.raises(ComputeEngineError, match="No QEMU runtime state"):
        eng.start_instance("web")


def test_start_refuses_when_a_pinned_port_was_taken(eng):
    import socket

    with socket.socket() as held:
        held.bind((HOST_IP, 0))
        held.listen(1)
        eng._write_runtime("web", _runtime(ssh_port=held.getsockname()[1]))
        with patch("app.engines.qemu.pid_alive", return_value=False):
            with pytest.raises(ComputeEngineError, match="pinned SSH port"):
                eng.start_instance("web")


def test_destroy_is_idempotent_for_an_unknown_instance(eng):
    eng.destroy_instance("never-existed")  # must not raise


def test_destroy_removes_the_instance_directory(eng):
    eng._write_runtime("web", _runtime(pid=None))
    (eng._dir("web") / "disk.qcow2").write_bytes(b"stub")
    with patch("app.engines.qemu.pid_alive", return_value=False):
        eng.destroy_instance("web")
    assert not eng._dir("web").exists()


# --------------------------------------------------------------------------- #
# Subprocess failure translation
# --------------------------------------------------------------------------- #
def test_missing_binary_raises_hypervisor_unavailable(eng):
    with patch("app.engines.qemu.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(HypervisorUnavailableError):
            eng._run(["qemu-img", "info"], timeout=5)


def test_timeout_raises_compute_timeout(eng):
    with patch(
        "app.engines.qemu.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="qemu-img", timeout=5),
    ):
        with pytest.raises(ComputeTimeoutError):
            eng._run(["qemu-img", "info"], timeout=5)


def test_nonzero_exit_carries_stderr(eng):
    with patch("app.engines.qemu.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Could not open backing file"
        )
        with pytest.raises(ComputeEngineError) as exc:
            eng._run(["qemu-img", "create"], timeout=5)

    assert exc.value.returncode == 1
    assert "backing file" in (exc.value.stderr or "")


def test_is_available_false_when_qemu_is_missing(eng):
    with patch("app.engines.qemu.subprocess.run", side_effect=FileNotFoundError()):
        assert eng.is_available() is False


def test_windows_does_not_get_a_different_accelerator(eng):
    """A Windows-specific TCG default lived here briefly and was withdrawn.

    It was added on "WHPX stalls, TCG progresses" and removed once longer runs
    showed TCG stalling too — further along, still writing nothing to disk. The
    default would have cost a ~30x slowdown and bought no working install, so
    the honest state is that guest OS does not steer the accelerator.
    """
    assert eng.resolve_accel(None, "windows") == eng.accel()
    assert eng.resolve_accel(None, "linux") == eng.accel()
    assert eng.resolve_accel(None) == eng.accel()


def test_an_explicit_accelerator_is_still_honoured(eng):
    assert eng.resolve_accel("tcg", "windows") == "tcg"
    assert eng.resolve_accel(eng.accel(), "windows") == eng.accel()


# --------------------------------------------------------------------------- #
# Cloning carries the guest family
# --------------------------------------------------------------------------- #
def _clone_engine(eng):
    """boot_cloned_instance with the two side effects that need a real VM
    stubbed out, so the runtime file it writes can be inspected directly."""
    return patch.object(eng, "_spawn"), patch.object(eng, "_wait_for_ssh", return_value=1.0)


def test_cloning_a_windows_instance_keeps_windows_hardware(eng):
    """The clone route passes guest_os and explains why; this method used to
    drop it, defaulting the runtime to Linux. That gave the copy a virtio root
    disk for a disk whose drivers bound to SATA — an unbootable VM, and one
    that would have looked like a mysterious boot failure rather than a
    dropped argument.
    """
    spawn, wait = _clone_engine(eng)
    with spawn, wait:
        eng.boot_cloned_instance(
            "winclone", cpus=2, memory="4096",
            options=LaunchOptions(guest_os="windows", display="std"),
        )
    runtime = eng._read_runtime("winclone")
    assert runtime is not None
    assert runtime.guest_os == "windows"

    cmd = eng.build_launch_command("winclone", runtime)
    assert "ich9-ahci,id=ahci" in cmd
    assert any(a.startswith("ide-hd,bus=ahci.0") for a in cmd)
    assert not any("if=virtio" in a for a in cmd)
    assert any(a.startswith("e1000e,") for a in cmd)


def test_a_windows_clone_is_not_waited_on_for_ssh(eng, settings):
    """Windows gets no NoCloud seed and therefore no key, so there is no SSH
    to become ready. Waiting anyway burns the whole boot timeout and then
    reports a healthy clone as a failure."""
    spawn, wait = _clone_engine(eng)
    with spawn, wait as wait_mock:
        eng.boot_cloned_instance(
            "winclone", cpus=2, memory="4096",
            options=LaunchOptions(guest_os="windows"),
        )
    wait_mock.assert_not_called()

    runtime = eng._read_runtime("winclone")
    assert runtime.ssh_enabled is False
    # No seed on disk, and none named on the command line.
    assert not (Path(settings.qemu_dir) / "instances" / "winclone" / "seed.iso").exists()
    assert not any("seed.iso" in a for a in eng.build_launch_command("winclone", runtime))


def test_cloning_a_linux_instance_is_unchanged(eng):
    """The fix must not quietly turn off cloud-init for the guests that use it."""
    spawn, wait = _clone_engine(eng)
    with spawn, patch.object(eng, "_write_seed") as seed, wait as wait_mock:
        eng.boot_cloned_instance("webclone", cpus=1, memory="1024")
    seed.assert_called_once()
    wait_mock.assert_called_once()

    runtime = eng._read_runtime("webclone")
    assert runtime.guest_os == "linux"
    assert runtime.ssh_enabled is True
    assert any("if=virtio" in a for a in eng.build_launch_command("webclone", runtime))


# --------------------------------------------------------------------------- #
# Liveness: a busy QMP socket is not a stopped VM
# --------------------------------------------------------------------------- #
def test_a_live_pid_with_a_silent_qmp_is_unreachable_not_stopped(eng):
    """QMP serves one client at a time.

    While anything else holds the socket, the handshake cannot complete. The
    old two-factor check read that as "not running" and the reconciler rewrote
    a healthy instance to Stopped and cleared its pid — observed as a 40-minute
    flap that stopped the moment the competing client disconnected.
    """
    runtime = _runtime(pid=4242)
    with patch("app.engines.qemu.pid_alive", return_value=True), \
         patch("app.engines.qemu.is_responsive", return_value=False), \
         patch("app.engines.qemu.is_port_free", return_value=False):
        assert eng._liveness(runtime) == "unreachable"
        assert eng._is_running(runtime) is True


def test_a_recycled_pid_is_still_caught(eng):
    """The case the two-factor check was built for, kept working.

    QEMU is gone, so its QMP port is bindable again — and the pid we recorded
    now belongs to some unrelated process. That is a genuinely stopped VM, and
    the port is what tells it apart from a merely busy monitor.
    """
    runtime = _runtime(pid=4242)
    with patch("app.engines.qemu.pid_alive", return_value=True), \
         patch("app.engines.qemu.is_responsive", return_value=False), \
         patch("app.engines.qemu.is_port_free", return_value=True):
        assert eng._liveness(runtime) == "stopped"
        assert eng._is_running(runtime) is False


def test_a_dead_pid_is_stopped_whatever_qmp_says(eng):
    runtime = _runtime(pid=4242)
    with patch("app.engines.qemu.pid_alive", return_value=False), \
         patch("app.engines.qemu.is_responsive", return_value=True):
        assert eng._liveness(runtime) == "stopped"
        assert eng._is_running(runtime) is False


def test_a_healthy_vm_is_running(eng):
    runtime = _runtime(pid=4242)
    with patch("app.engines.qemu.pid_alive", return_value=True), \
         patch("app.engines.qemu.is_responsive", return_value=True):
        assert eng._liveness(runtime) == "running"
        assert eng._is_running(runtime) is True


def test_a_busy_monitor_does_not_let_a_clone_read_a_live_disk(eng, tmp_path):
    """_refuse_if_running guards clone and snapshot against reading a disk a
    running QEMU is writing. Treating a busy monitor as stopped would have
    quietly removed that guard exactly when another tool was poking the VM."""
    (eng._dir("web")).mkdir(parents=True, exist_ok=True)
    eng._write_runtime("web", _runtime(pid=4242))
    with patch("app.engines.qemu.pid_alive", return_value=True), \
         patch("app.engines.qemu.is_responsive", return_value=False), \
         patch("app.engines.qemu.is_port_free", return_value=False):
        with pytest.raises(ComputeEngineError):
            eng._refuse_if_running("web", "clone")


# --------------------------------------------------------------------------- #
# USB HID input — without it the Windows console cannot be driven at all
# --------------------------------------------------------------------------- #
def test_windows_gets_a_usb_keyboard_and_tablet(eng):
    """QMP send-key does not reach a PS/2 keyboard with -display none and no
    VNC client attached. Measured: a whole Setup key sequence sent blind left
    the disk at its initial 393,216 bytes, while the same sequence with USB HID
    attached drove Setup screen by screen. Windows has no SSH, so the console is
    the only way in and this is what makes it work.
    """
    cmd = eng.build_launch_command("win", _runtime(guest_os="windows"))
    assert "qemu-xhci,id=xhci" in cmd
    assert "usb-kbd,bus=xhci.0" in cmd
    assert "usb-tablet,bus=xhci.0" in cmd


def test_the_usb_controller_precedes_the_devices_that_reference_it(eng):
    """`bus=xhci.0` must resolve to something already on the command line, the
    same ordering rule the AHCI controller follows."""
    cmd = eng.build_launch_command("win", _runtime(guest_os="windows"))
    assert cmd.index("qemu-xhci,id=xhci") < cmd.index("usb-kbd,bus=xhci.0")
    assert cmd.index("qemu-xhci,id=xhci") < cmd.index("usb-tablet,bus=xhci.0")


def test_linux_keeps_ps2_and_gains_no_usb_devices(eng):
    """Linux guests are reached over SSH and already work. Adding devices would
    change the hardware under every instance already on disk for no gain."""
    cmd = eng.build_launch_command("web", _runtime())
    assert not any("xhci" in a or "usb-kbd" in a or "usb-tablet" in a for a in cmd)


def test_a_windows_clone_also_gets_usb_input(eng):
    """The clone carries guest_os, so it must carry the console hardware too —
    a clone nobody can type into is not a usable clone."""
    with patch.object(eng, "_spawn"), patch.object(eng, "_wait_for_ssh", return_value=1.0):
        eng.boot_cloned_instance(
            "winclone", cpus=2, memory="2048",
            options=LaunchOptions(guest_os="windows"),
        )
    cmd = eng.build_launch_command("winclone", eng._read_runtime("winclone"))
    assert "usb-kbd,bus=xhci.0" in cmd
