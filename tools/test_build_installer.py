"""
Tests for the build script, which is itself a shipped artefact.

The two things checked hardest are the two the brief and the owner asked for
after they had each already caused a real failure: the path-length refusal, and
the QEMU checksum manifest. Both are guards, and a guard that cannot fail is
worse than no guard — so each is tested by actually breaking the thing it
guards.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

import build_installer
from build_installer import (
    clear_previous_artefacts,
    MANIFEST_NAME,
    MAX_BUILD_ROOT,
    MAX_PATH,
    BuildError,
    Manifest,
    check_build_root,
    sha256,
    verify_qemu,
)


# --------------------------------------------------------------------------- #
# The path budget
# --------------------------------------------------------------------------- #
def test_a_short_root_is_accepted(tmp_path: Path):
    check_build_root(tmp_path if len(str(tmp_path)) <= MAX_BUILD_ROOT else Path("C:/kk"))


def test_the_budget_leaves_room_for_the_deepest_measured_path():
    """The arithmetic, asserted rather than trusted to a comment.

    A build root at exactly the budget plus the deepest path this build is known
    to create must still fit inside MAX_PATH, with the reserved headroom intact.
    """
    from build_installer import _HEADROOM, _MEASURED_DEEPEST

    assert MAX_BUILD_ROOT + 1 + _MEASURED_DEEPEST + _HEADROOM == MAX_PATH


@pytest.mark.skipif(sys.platform != "win32", reason="the 260-character limit is a Windows rule, and C:/ is a relative path elsewhere")
def test_the_path_that_actually_broke_this_project_is_refused(monkeypatch):
    """159 characters, which produced a 262-character path against a 260 limit.

    Not a hypothetical. The first frozen build of this project failed on
    `_internal/setuptools/_vendor/importlib_metadata-8.7.1.dist-info/licenses`
    from a build root of exactly this length and shape — a temp directory, a
    slug of the checkout's own path, and a per-run UUID.

    The path below is synthetic. The real one named a username and a session
    id, and a test fixture is a poor reason to publish either; the length is the
    only part the check cares about, and it is asserted rather than assumed so
    the fixture cannot drift away from the case it represents.
    """
    monkeypatch.setattr("build_installer.long_paths_enabled", lambda: False)
    offender = Path(
        "C:/Users/builder/AppData/Local/Temp/build-sandbox/"
        "C--builder-Projects-kurukuru-phase1-kurukuru-release-1/"
        "00000000-0000-0000-0000-000000000000/scratchpad/frozen"
    )
    assert len(str(offender)) == 159

    with pytest.raises(BuildError) as exc:
        check_build_root(offender)

    message = str(exc.value)
    # The message has to carry the arithmetic: "use a shorter path" is not
    # actionable when you cannot see how much shorter.
    assert "159 characters" in message
    assert f"budget   {MAX_BUILD_ROOT}" in message
    assert "over by  26" in message
    assert "LongPathsEnabled" in message


def test_long_path_support_makes_the_check_advisory(monkeypatch, capsys):
    """When the machine has opted out of the limit, enforcing it is superstition."""
    monkeypatch.setattr("build_installer.long_paths_enabled", lambda: True)

    check_build_root(Path("C:/" + "x" * (MAX_BUILD_ROOT + 20)))

    assert "long paths are enabled" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The QEMU manifest
# --------------------------------------------------------------------------- #
@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """A collected bundle with a valid manifest."""
    target = tmp_path / "qemu"
    (target / "share").mkdir(parents=True)
    (target / "qemu-img.exe").write_bytes(b"MZ binary")
    (target / "COPYING").write_text("GPL v2", encoding="utf-8")
    (target / "share" / "bios-256k.bin").write_bytes(b"firmware")

    manifest = Manifest(qemu_version="11.1.0", source="test", built_at="now")
    for path in sorted(target.rglob("*")):
        if path.is_file():
            manifest.files[path.relative_to(target).as_posix()] = sha256(path)
    (target / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
    return target


def test_a_clean_bundle_verifies(bundle: Path):
    assert verify_qemu(bundle) == 3


def test_a_changed_byte_is_caught(bundle: Path):
    """Antivirus quarantine, a partial copy, or a swapped binary.

    One bit is enough, which is the point of hashing rather than comparing
    sizes: a truncated file and a substituted one of the same length both pass a
    size check.
    """
    firmware = bundle / "share" / "bios-256k.bin"
    firmware.write_bytes(b"firmwarX")

    with pytest.raises(BuildError, match="changed"):
        verify_qemu(bundle)


def test_a_truncated_binary_is_caught(bundle: Path):
    (bundle / "qemu-img.exe").write_bytes(b"MZ")

    with pytest.raises(BuildError, match="changed"):
        verify_qemu(bundle)


def test_a_missing_licence_is_caught(bundle: Path):
    """QEMU is GPLv2; a bundle without its licence text is not distributable."""
    (bundle / "COPYING").unlink()

    with pytest.raises(BuildError, match="missing"):
        verify_qemu(bundle)


def test_an_unrecorded_file_is_caught(bundle: Path):
    """As much a problem as a missing one: it would ship unchecked."""
    (bundle / "share" / "unexpected.dll").write_bytes(b"never hashed")

    with pytest.raises(BuildError, match="unrecorded"):
        verify_qemu(bundle)


def test_verification_needs_a_manifest_to_mean_anything(tmp_path: Path):
    """Without this, a bundle whose manifest was lost would verify vacuously."""
    (tmp_path / "qemu-img.exe").write_bytes(b"MZ")

    with pytest.raises(BuildError, match=MANIFEST_NAME):
        verify_qemu(tmp_path)


def test_the_manifest_records_the_pinned_version(bundle: Path):
    recorded = json.loads((bundle / MANIFEST_NAME).read_text(encoding="utf-8"))

    assert recorded["qemu_version"] == "11.1.0"
    assert recorded["file_count"] == 3


def test_the_pin_matches_what_the_product_claims_to_support():
    """A pin the code disagrees with makes the dashboard's badge a lie."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    from kurukuru.config import Settings
    from build_installer import PINNED_QEMU_VERSION

    assert PINNED_QEMU_VERSION == Settings().qemu_version_max_tested


def test_qemu_licences_are_not_optional():
    """Named in the collection list, so forgetting them fails the build."""
    from build_installer import QEMU_LICENCES

    assert "COPYING" in QEMU_LICENCES


# --------------------------------------------------------------------------- #
# Not leaving last run's artefacts beside this run's
# --------------------------------------------------------------------------- #
def test_previous_artefacts_are_cleared_before_a_build(tmp_path):
    """The trap this closes: three files, one version, different bytes.

    C:\kk-build\dist really did hold Kurukuru-0.1.0-Setup.exe alongside a
    .previous and a .stale, all claiming 0.1.0, two of them predating a fix to
    the code that protects the API token. They tab-complete alike, so the only
    thing standing between that directory and the wrong file being published
    was somebody checking a checksum they had no reason to doubt.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in (
        "Kurukuru-0.1.0-Setup.exe",
        "Kurukuru-0.1.0-Setup.exe.previous",
        "Kurukuru-0.1.0-Setup.exe.stale",
        "Kurukuru-0.1.0-Setup.exe.sha256",
        "Kurukuru-0.1.1-Setup.exe",
    ):
        (dist / name).write_text("x")

    removed = clear_previous_artefacts(dist)

    assert len(removed) == 5
    assert list(dist.glob("Kurukuru-*")) == []


def test_clearing_leaves_unrelated_files_alone(tmp_path):
    """Scoped to the artefact name on purpose.

    The directory comes from --build-root, so emptying it wholesale would be a
    worse failure than the one being prevented.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "Kurukuru-0.1.0-Setup.exe").write_text("artefact")
    (dist / "notes.txt").write_text("keep me")
    (dist / "subdir").mkdir()

    clear_previous_artefacts(dist)

    assert (dist / "notes.txt").is_file()
    assert (dist / "subdir").is_dir()


def test_clearing_a_directory_that_does_not_exist_is_not_an_error(tmp_path):
    """First build on a fresh machine. Nothing to clear is not a failure."""
    assert clear_previous_artefacts(tmp_path / "never-created") == []


# --------------------------------------------------------------------------- #
# The version resource on the frozen executable
# --------------------------------------------------------------------------- #
def test_the_version_resource_carries_a_matching_name_and_version(tmp_path):
    """ProductName and ProductVersion, generated from the one definition.

    `kurukuru.exe` shipped in 0.1.0, 0.1.1 and 0.1.2 with **no version resource
    at all** — blank ProductName, blank ProductVersion — because PyInstaller
    adds none unless handed one. Nothing looked at it: the installer carries
    its own metadata, the dashboard reports the version over HTTP, and
    `kurukuru version` reads installed package metadata. Code signing is what
    surfaced it, since SignPath requires a matching ProductName and a
    consistent ProductVersion on every signed artifact.
    """
    resource = build_installer.write_version_resource(tmp_path)
    text = resource.read_text(encoding="utf-8")

    release = build_installer.version()
    name = build_installer.product_string("PRODUCT_NAME")
    publisher = build_installer.installer_define("AppPublisher")

    assert f"StringStruct('ProductName', '{name}')" in text
    assert f"StringStruct('ProductVersion', '{release}')" in text
    assert f"StringStruct('FileVersion', '{release}')" in text
    assert f"StringStruct('CompanyName', '{publisher}')" in text


def test_the_binary_version_quad_is_four_integers(tmp_path, monkeypatch):
    """Win32 wants four numbers, whatever the release string looks like.

    A working tree reports "0.0.0-dev", and a suffix is not a number. The
    string fields keep the full version because that is the one a human reads;
    the fixed-info quad has to be numeric or the resource will not compile.
    """
    import re

    for release, expected in (
        ("0.1.2", "0, 1, 2, 0"),
        ("0.0.0-dev", "0, 0, 0, 0"),
        ("1.2.3.4", "1, 2, 3, 4"),
        ("2.0", "2, 0, 0, 0"),
    ):
        monkeypatch.setattr(build_installer, "version", lambda r=release: r)
        text = build_installer.write_version_resource(tmp_path).read_text(encoding="utf-8")
        assert f"filevers=({expected})" in text, release
        assert f"prodvers=({expected})" in text, release
        # And the human-readable field keeps the suffix.
        assert f"StringStruct('FileVersion', '{release}')" in text


def test_the_publisher_agrees_with_the_installer_script():
    """One publisher, read from the file Inno stamps the installer from.

    Two artifacts in one release disagreeing about who published them is
    exactly the inconsistency a signing service checks for.
    """
    assert build_installer.installer_define("AppPublisher")
    assert build_installer.installer_define("AppName") == build_installer.product_string(
        "PRODUCT_NAME"
    )


def _iss_section(name: str) -> list[str]:
    text = (build_installer.REPO / "packaging" / "windows" / "kurukuru.iss").read_text(
        encoding="utf-8"
    )
    lines, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped == f"[{name}]"
            continue
        if inside and stripped and not stripped.startswith(";"):
            lines.append(stripped)
    return lines


def test_every_directory_installed_wholesale_is_cleared_first():
    """An upgrade must not carry the last version's files forward.

    Inno's [Files] copies over the top and never removes a file the new version
    stopped shipping. 0.1.2 -> 0.1.4 left 51 behind, including FastAPI
    0.115.12's dist-info next to 0.120.4's and its ``_compat.py`` next to the
    ``_compat`` package that replaced it — in the release whose whole point
    was the framework upgrade. Every directory [Files] fills recursively is a
    directory this product owns outright, so each must be wiped by
    [InstallDelete] first. Derived from [Files] rather than listed, so a new
    one is covered the day it is added.
    """
    import re

    wholesale = set()
    for entry in _iss_section("Files"):
        if "recursesubdirs" not in entry:
            continue
        dest = re.search(r'DestDir:\s*"([^"]+)"', entry).group(1)
        if dest == "{app}":
            # The frozen app's own tree: what it fills under {app} is _internal.
            wholesale.add("{app}\\_internal")
        else:
            wholesale.add(dest)

    cleared = {
        re.search(r'Name:\s*"([^"]+)"', entry).group(1)
        for entry in _iss_section("InstallDelete")
        if "filesandordirs" in entry
    }

    assert wholesale, "found no recursive [Files] entries — has the script's shape changed?"
    assert wholesale <= cleared, (
        f"Installed wholesale but never cleared before an upgrade: {sorted(wholesale - cleared)}"
    )


def test_install_delete_never_reaches_beyond_the_product_directories():
    """The other direction, and the one that would destroy something.

    A wipe of ``{app}`` itself, or of anything outside it, could take a file
    the user put there, or worse. Only named subdirectories of ``{app}`` that
    the product rebuilds in full are allowed.
    """
    import re

    for entry in _iss_section("InstallDelete"):
        name = re.search(r'Name:\s*"([^"]+)"', entry).group(1)
        assert name.startswith("{app}\\") and name.count("\\") == 1, entry
        assert name.split("\\", 1)[1] in {"_internal", "dashboard", "qemu"}, entry


# --------------------------------------------------------------------------- #
# The icon the binaries carry
# --------------------------------------------------------------------------- #
def _embed_icon(exe: Path, icon: Path) -> None:
    """Embed ``icon`` into ``exe`` the way PyInstaller and Inno do: one RT_ICON
    per frame, plus the RT_GROUP_ICON directory that indexes them."""
    import ctypes
    import struct
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.BeginUpdateResourceW.restype = wintypes.HANDLE
    k32.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    k32.UpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID,
                                    wintypes.WORD, ctypes.c_void_p, wintypes.DWORD]
    k32.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]

    data = icon.read_bytes()
    _, _, count = struct.unpack_from("<HHH", data, 0)
    handle = k32.BeginUpdateResourceW(str(exe), True)  # True: drop existing resources
    assert handle, ctypes.get_last_error()
    group = struct.pack("<HHH", 0, 1, count)
    for i in range(count):
        w, h, colours, reserved, planes, bpp, size, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * i
        )
        frame = data[offset:offset + size]
        buf = ctypes.create_string_buffer(frame, len(frame))
        assert k32.UpdateResourceW(handle, ctypes.c_void_p(3), ctypes.c_void_p(i + 1), 0, buf, len(frame))
        group += struct.pack("<BBBBHHIH", w, h, colours, reserved, planes, bpp, size, i + 1)
    gbuf = ctypes.create_string_buffer(group, len(group))
    assert k32.UpdateResourceW(handle, ctypes.c_void_p(14), ctypes.c_void_p(1), 0, gbuf, len(group))
    assert k32.EndUpdateResourceW(handle, False), ctypes.get_last_error()


@pytest.mark.skipif(sys.platform != "win32", reason="reads PE resources through kernel32")
def test_a_binary_carrying_the_product_icon_is_accepted(tmp_path: Path):
    """A real PE with kurukuru.ico embedded, built the way the release builds
    embed it — so the check is exercised against resources, not a mock."""
    exe = tmp_path / "carries-icon.exe"
    shutil.copy2(sys.executable, exe)
    _embed_icon(exe, build_installer.PRODUCT_ICON)

    build_installer.verify_icon(exe)


@pytest.mark.skipif(sys.platform != "win32", reason="reads PE resources through kernel32")
def test_a_binary_with_some_other_icon_is_refused(tmp_path: Path):
    """The deliberate break: python.exe carries Python's icon, which is exactly
    the shape of a build that dropped --icon and shipped its packager's own.
    (Also checked against the real thing: the 0.1.4 build made before --icon
    was added is refused, 0 of 7 frames shared — DECISIONS #66.)"""
    exe = tmp_path / "other-icon.exe"
    shutil.copy2(sys.executable, exe)

    with pytest.raises(build_installer.BuildError, match="does not carry kurukuru.ico"):
        build_installer.verify_icon(exe)


def test_the_frozen_backend_is_built_with_the_product_icon(tmp_path: Path, monkeypatch):
    """--icon on the PyInstaller command line, pointing at the product icon,
    and the frozen executable then checked — both, because either alone was
    missing for every release before 0.1.4."""
    commands: list[list[str]] = []
    checked: list[Path] = []

    def fake_run(command, *, cwd):  # noqa: ANN001
        commands.append(command)
        (tmp_path / "stage" / "dist" / "kurukuru").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(build_installer, "run", fake_run)
    monkeypatch.setattr(build_installer, "verify_icon", lambda exe, icon=None: checked.append(exe))
    monkeypatch.setattr(build_installer, "write_version_resource", lambda out: out / "v.txt")
    (tmp_path / "stage").mkdir()

    frozen = build_installer.freeze_backend(tmp_path / "stage", tmp_path / "work")

    pyinstaller = commands[0]
    assert pyinstaller[pyinstaller.index("--icon") + 1] == str(build_installer.PRODUCT_ICON)
    assert checked == [frozen / "kurukuru.exe"]
