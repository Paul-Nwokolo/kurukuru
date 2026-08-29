"""Version awareness and capability probing.

The point of these tests is the *separation*: a version number and a capability
are different questions, and the failures that shaped Phase 13 are all invisible
to the first one. A build can be perfectly in range and still be unable to give
a guest a TPM, and a check that conflated the two would report "supported" over
an install that cannot work.
"""

from __future__ import annotations

import pathlib

import pytest

from kurukuru.config import Settings
from kurukuru.engines import capabilities as caps


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_a_release_version_parses_to_numbers():
    version = caps.parse_version("QEMU emulator version 9.2.0")
    assert version is not None
    assert version.release == (9, 2, 0)
    assert version.text == "9.2.0"
    assert not version.is_prerelease


def test_a_development_build_is_recognised_as_one():
    """The build actually installed on the development host.

    Note the numbers: 10.0.94 from a tree described as v10.1.0-rc4-12093-g…, so
    it is numerically *below* 10.1.0 while containing 12k commits more than the
    rc. No comparison against a range says anything useful about it, which is
    exactly why this is a separate flag rather than a version rule.
    """
    version = caps.parse_version(
        "QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gbd0a254583)"
    )
    assert version is not None
    assert version.release == (10, 0, 94)
    assert version.is_prerelease


def test_unparseable_output_is_none_rather_than_a_guess():
    assert caps.parse_version("") is None
    assert caps.parse_version("command not found") is None


# --------------------------------------------------------------------------- #
# Version range
# --------------------------------------------------------------------------- #
def _support(monkeypatch, version_output: str, **overrides) -> caps.QemuSupport:
    """Drive probe_support against canned QEMU output."""
    responses = {
        "--version": (0, version_output),
        "-tpmdev": (0, "-tpmdev: invalid option"),
        "-device": (0, "name \"ich9-ahci\"\nname \"e1000e\"\n"),
    }

    def fake_probe(binary, args, timeout):
        return responses.get(args[0], (0, ""))

    monkeypatch.setattr(caps, "_probe", fake_probe)
    settings = Settings(**overrides)
    return caps.probe_support(settings)


def test_a_version_in_range_is_ok(monkeypatch):
    support = _support(
        monkeypatch,
        "QEMU emulator version 9.2.0",
        qemu_version_min="8.0.0",
        qemu_version_max_tested="10.1.0",
    )
    assert support.status == "ok"
    assert support.supported


def test_a_version_below_the_minimum_warns_and_says_to_upgrade(monkeypatch):
    support = _support(
        monkeypatch,
        "QEMU emulator version 6.1.0",
        qemu_version_min="8.0.0",
        qemu_version_max_tested="10.1.0",
    )
    assert support.status == "too-old"
    assert not support.supported
    assert any("below the minimum" in w for w in support.warnings)


def test_a_version_above_the_tested_maximum_warns_without_alarm(monkeypatch):
    """Newer is a caveat, not a fault — the warning has to read like one."""
    support = _support(
        monkeypatch,
        "QEMU emulator version 11.0.0",
        qemu_version_min="8.0.0",
        qemu_version_max_tested="10.1.0",
    )
    assert support.status == "untested"
    assert any("probably work" in w.lower() for w in support.warnings)


def test_a_prerelease_wins_over_the_untested_comparison(monkeypatch):
    """Both could apply; only one is worth saying.

    Calling a 12k-commit snapshot "newer than tested" is precise and useless.
    That it is unreleased is the fact that changes what the user should do.
    """
    support = _support(
        monkeypatch,
        "QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gbd0a254583)",
        qemu_version_min="8.0.0",
        qemu_version_max_tested="10.1.0",
    )
    assert support.status == "prerelease"
    assert any("development build" in w for w in support.warnings)


def test_an_unreadable_binary_is_unknown_not_supported(monkeypatch):
    monkeypatch.setattr(caps, "_probe", lambda b, a, t: (-1, "No such file"))
    support = caps.probe_support(Settings())
    assert support.status == "unknown"
    assert not support.supported
    assert support.version is None


# --------------------------------------------------------------------------- #
# Capabilities — the half a version cannot answer
# --------------------------------------------------------------------------- #
def test_an_in_range_build_can_still_lack_tpm(monkeypatch):
    """The finding that motivated the whole feature.

    QEMU gates TPM on `host_os != 'windows'` in meson.build, so a current,
    in-range, perfectly healthy Windows build has no TPM at all — and Windows 11
    cannot install. Reporting the version alone would have called this fine.
    """
    support = _support(
        monkeypatch,
        "QEMU emulator version 9.2.0",
        qemu_version_min="8.0.0",
        qemu_version_max_tested="10.1.0",
    )

    assert support.status == "ok"  # the version really is fine
    tpm = support.capability("tpm")
    assert tpm is not None and not tpm.available
    assert "Windows 11" in (tpm.consequence or "")
    # And it reaches the warning list, so a caller showing only warnings sees it.
    assert any("Windows 11" in w for w in support.warnings)


def test_tpm_is_reported_available_when_the_option_is_accepted(monkeypatch):
    monkeypatch.setattr(
        caps, "_probe",
        lambda b, a, t: (0, "QEMU emulator version 9.2.0") if a[0] == "--version"
        else (0, "tpmdev backends: emulator passthrough") if a[0] == "-tpmdev"
        else (0, 'name "ich9-ahci"\nname "e1000e"'),
    )
    support = caps.probe_support(Settings())
    tpm = support.capability("tpm")
    assert tpm is not None and tpm.available


def test_missing_windows_devices_are_named_individually(monkeypatch):
    monkeypatch.setattr(
        caps, "_probe",
        lambda b, a, t: (0, "QEMU emulator version 9.2.0") if a[0] == "--version"
        else (0, "-tpmdev: invalid option") if a[0] == "-tpmdev"
        else (0, 'name "ich9-ahci"'),  # e1000e absent
    )
    support = caps.probe_support(Settings())
    devices = support.capability("windows_devices")
    assert devices is not None and not devices.available
    assert "e1000e" in devices.detail
    assert "ich9-ahci" not in devices.detail  # only what is actually missing


def test_uefi_is_reported_unusable_under_whpx_even_when_present(tmp_path, monkeypatch):
    """Firmware sitting exactly where it belongs, and still not bootable.

    WHPX cannot emulate OVMF's pflash MMIO access — an upstream bug open since
    2019. "The file is there" is the wrong question, and answering it would send
    someone looking for a missing file that is not missing.
    """
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"")
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "edk2-x86_64-code.fd").write_bytes(b"")

    capability = caps._uefi_capability(str(binary), accel="whpx")
    assert not capability.available
    assert "WHPX" in capability.detail or "WHPX" in (capability.consequence or "")

    # Under software emulation the firmware is usable by QEMU — but no instance
    # can boot it, because the engine emits no pflash arguments. Both cases are
    # therefore unavailable, and the *reason* is what has to differ: only the
    # WHPX one is a build defect, so only it carries a consequence.
    tcg = caps._uefi_capability(str(binary), accel="tcg")
    assert not tcg.available
    assert "not wired" in tcg.detail
    assert "WHPX" not in tcg.detail
    assert tcg.consequence is None
    assert capability.consequence is not None


def test_firmware_is_found_when_the_binary_is_a_bare_name_on_path(tmp_path, monkeypatch):
    """The configured binary is normally "qemu-system-x86_64", not a full path.

    `Path("qemu-system-x86_64").parent` is `.` — the backend's working
    directory — so looking for firmware "beside the binary" looked beside the
    wrong thing entirely and reported OVMF missing on a host where it was
    measurably present. Caught by running the probe against a real install
    rather than against the canned output above, which is the whole argument
    for doing that.
    """
    install = tmp_path / "Program Files" / "qemu"
    (install / "share").mkdir(parents=True)
    (install / "share" / "edk2-x86_64-code.fd").write_bytes(b"")
    (install / "qemu-system-x86_64.exe").write_bytes(b"")

    monkeypatch.setattr(
        caps.shutil, "which", lambda name: str(install / "qemu-system-x86_64.exe")
    )
    capability = caps._uefi_capability("qemu-system-x86_64", accel="tcg")

    # `available` no longer distinguishes found from missing — nothing can boot
    # UEFI either way — so this asserts on what the probe actually looked up.
    assert "edk2-x86_64-code.fd" in capability.detail, capability.detail
    assert "no edk2" not in capability.detail


def test_uefi_absent_is_a_different_message_from_uefi_unusable(tmp_path):
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"")
    capability = caps._uefi_capability(str(binary), accel="tcg")
    assert not capability.available
    assert "no edk2" in capability.detail


def test_uefi_stops_being_a_build_defect_on_the_version_that_fixed_pflash(tmp_path):
    """The 2019 pflash bug is fixed, and the check has to be able to say so.

    Measured: 10.0.94 died instantly with "Failed to emulate MMIO access";
    11.1.0 boots OVMF through pflash under WHPX all the way to a Linux kernel
    console, and its NVRAM persists (DECISIONS #35). A permanently-hardcoded
    "WHPX means no UEFI" would now be telling users to avoid something that
    works at the QEMU level.

    Both builds still report unavailable, because no instance can boot UEFI
    on either — the engine emits no pflash arguments. What changes across the
    threshold is the *kind* of unavailable: a build defect with a consequence
    below the threshold, an unbuilt feature with none above it.
    """
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"")
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "edk2-x86_64-code.fd").write_bytes(b"")

    old = caps.parse_version("QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-ga1b2c3d)")
    new = caps.parse_version("QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)")

    before = caps._uefi_capability(str(binary), "whpx", old)
    after = caps._uefi_capability(str(binary), "whpx", new)

    assert not before.available and not after.available
    assert before.consequence is not None      # the build cannot do it
    assert after.consequence is None           # the build can; we have not wired it
    assert "not wired" in after.detail
    # The detail carries the boundary, because "it works now" is only useful
    # alongside "and it did not before".
    assert "11.1.0" in after.detail


def test_the_old_whpx_warning_names_the_version_that_fixes_it(tmp_path):
    binary = tmp_path / "qemu-system-x86_64.exe"
    binary.write_bytes(b"")
    (tmp_path / "share").mkdir()
    (tmp_path / "share" / "edk2-x86_64-code.fd").write_bytes(b"")

    old = caps.parse_version("QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-ga1b2c3d)")
    capability = caps._uefi_capability(str(binary), "whpx", old)

    assert "11.1.0" in (capability.consequence or "")


# --------------------------------------------------------------------------- #
# Toolchain consistency — the failure that trusting PATH order actually caused
# --------------------------------------------------------------------------- #
def _toolchain(monkeypatch, tmp_path, system_dir, img_dir, system_ver, img_ver):
    """Build two fake installs and run the toolchain check across them."""
    paths = {}
    for name, directory, version in (
        ("qemu-system-x86_64", system_dir, system_ver),
        ("qemu-img", img_dir, img_ver),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / f"{name}.exe"
        binary.write_bytes(b"")
        paths[str(binary)] = version

    resolved = list(paths)
    monkeypatch.setattr(
        caps.shutil, "which",
        lambda name: next(p for p in resolved if pathlib.Path(p).stem == name),
    )
    monkeypatch.setattr(
        caps, "_probe", lambda binary, args, timeout: (0, paths.get(binary, ""))
    )
    settings = Settings(
        qemu_system_binary="qemu-system-x86_64", qemu_img_binary="qemu-img"
    )
    return caps._toolchain_capability(settings, timeout=5)


def test_one_install_is_consistent(monkeypatch, tmp_path):
    same = tmp_path / "qemu"
    capability = _toolchain(
        monkeypatch, tmp_path, same, same,
        "QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)",
        "qemu-img version 11.1.0 (v11.1.0-12130-ge470268ff4)",
    )
    assert capability.available, capability.detail
    assert "11.1.0" in capability.detail


def test_a_shadowing_qemu_img_is_caught(monkeypatch, tmp_path):
    """The real bug: Multipass's 8.0.0 qemu-img shadowed the real one on PATH.

    Both binaries answered `--version` happily about themselves, so nothing
    noticed for the whole project. Only comparing them finds it.
    """
    capability = _toolchain(
        monkeypatch, tmp_path, tmp_path / "qemu", tmp_path / "Multipass" / "bin",
        "QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)",
        "qemu-img version 8.0.0 (v8.0.0-dirty)",
    )
    assert not capability.available
    # Both sides are named, because "they disagree" without saying which is
    # where leaves the user with nothing to fix.
    assert "Multipass" in capability.detail
    assert "8.0.0" in capability.detail and "11.1.0" in capability.detail
    assert "KURUKURU_QEMU_IMG_BINARY" in (capability.consequence or "")


def test_same_version_from_two_directories_is_still_reported(monkeypatch, tmp_path):
    """Benign today, but it means PATH is deciding something nobody chose."""
    version = "QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)"
    capability = _toolchain(
        monkeypatch, tmp_path, tmp_path / "a", tmp_path / "b",
        version, version.replace("QEMU emulator", "qemu-img"),
    )
    assert not capability.available
    assert "different directories" in capability.detail


def test_a_missing_image_tool_says_which_one(monkeypatch, tmp_path):
    """Only qemu-img is unfindable; the message must not blame the other one."""
    system = tmp_path / "qemu-system-x86_64.exe"
    system.write_bytes(b"")
    monkeypatch.setattr(
        caps.shutil, "which",
        lambda name: str(system) if name == "qemu-system-x86_64" else None,
    )
    monkeypatch.setattr(caps, "_probe", lambda b, a, t: (0, "version 11.1.0"))

    capability = caps._toolchain_capability(
        Settings(qemu_img_binary="definitely-not-qemu-img"), timeout=5
    )

    assert not capability.available
    assert "definitely-not-qemu-img" in capability.detail
    assert "qemu-system-x86_64" not in capability.detail


def test_both_missing_names_both(monkeypatch):
    monkeypatch.setattr(caps.shutil, "which", lambda name: None)
    capability = caps._toolchain_capability(Settings(), timeout=5)
    assert not capability.available
    assert "qemu-system-x86_64" in capability.detail
    assert "qemu-img" in capability.detail


def test_the_cache_key_includes_the_image_tool(tmp_path, monkeypatch):
    """Left out, a caller pointing qemu-img somewhere new gets the old answer."""
    monkeypatch.setattr(
        caps, "_probe",
        lambda b, a, t: (0, "QEMU emulator version 9.2.0") if a[0] == "--version"
        else (0, "-tpmdev: invalid option") if a[0] == "-tpmdev"
        else (0, 'name "ich9-ahci"\nname "e1000e"'),
    )
    caps._SUPPORT_CACHE.clear()
    caps.cached_support(Settings(qemu_img_binary="one"))
    caps.cached_support(Settings(qemu_img_binary="two"))
    assert len(caps._SUPPORT_CACHE) == 2


# --------------------------------------------------------------------------- #
# Advisory, never a gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", ["too-old", "untested", "prerelease", "unknown"])
def test_no_verdict_raises(monkeypatch, status):
    """Nothing here may refuse anything.

    A warning that turns out to be wrong costs a sentence. A gate that turns out
    to be wrong costs the user their VM, and every one of these verdicts is a
    heuristic over a version string.
    """
    outputs = {
        "too-old": "QEMU emulator version 1.0.0",
        "untested": "QEMU emulator version 99.0.0",
        "prerelease": "QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gabcdef1)",
        "unknown": "not a version at all",
    }
    support = _support(monkeypatch, outputs[status])
    assert support.status == status
    assert isinstance(support.as_dict(), dict)


def test_the_cache_uses_the_settings_it_was_given(tmp_path, monkeypatch):
    """Regression: the memo cached on a key derived from the caller's settings
    but then computed the value from the global ones, so a caller with its own
    settings silently probed the real configuration instead."""
    seen: list[str] = []

    def fake_probe(binary, args, timeout):
        if args[0] == "--version":
            seen.append(binary)
            return 0, "QEMU emulator version 9.2.0"
        return 0, "-tpmdev: invalid option" if args[0] == "-tpmdev" else 'name "ich9-ahci"\nname "e1000e"'

    monkeypatch.setattr(caps, "_probe", fake_probe)
    caps._SUPPORT_CACHE.clear()
    mine = Settings(qemu_system_binary=str(tmp_path / "my-qemu"))

    caps.cached_support(mine)

    assert seen and seen[0] == mine.qemu_system_binary
