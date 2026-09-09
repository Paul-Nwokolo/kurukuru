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
from pathlib import Path

import pytest

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
