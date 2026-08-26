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

    Not a hypothetical: this is the build root of the first frozen build of this
    project, which failed on
    `_internal/setuptools/_vendor/importlib_metadata-8.7.1.dist-info/licenses`.
    """
    monkeypatch.setattr("build_installer.long_paths_enabled", lambda: False)
    offender = Path(
        "C:/Users/nwoko/AppData/Local/Temp/claude/"
        "C--Users-nwoko-Project-Abstraction-local-iaas-phase1-local-iaas/"
        "a7546011-3638-46ed-9a65-6900f6dacc89/scratchpad/frozen"
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
