"""
Tests for the boot-ISO catalog.

The listing is simple; the resolver is not, and it is the part that matters. A
filename arrives from an HTTP request and ends up as a QEMU ``-drive file=``
argument, so an escape here means attaching an arbitrary host file to a guest as
a readable CD-ROM. Every escape shape gets its own case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kurukuru.config import Settings
from kurukuru.isos import IsoError, list_isos, resolve_iso


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    (tmp_path / "isos").mkdir()
    return Settings(iso_dir=str(tmp_path / "isos"))


def _make_iso(settings: Settings, name: str, size: int = 2048) -> Path:
    path = Path(settings.iso_dir) / name
    path.write_bytes(b"\0" * size)
    return path


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def test_missing_directory_lists_nothing(tmp_path: Path):
    """A fresh install has no ISO dir; that is empty, not an error."""
    assert list_isos(Settings(iso_dir=str(tmp_path / "absent"))) == []


def test_lists_only_iso_files(settings: Settings):
    _make_iso(settings, "alpine.iso")
    _make_iso(settings, "ubuntu.ISO")  # case-insensitive suffix
    (Path(settings.iso_dir) / "notes.txt").write_text("not an iso")
    (Path(settings.iso_dir) / "nested").mkdir()

    names = {i.name for i in list_isos(settings)}
    assert names == {"alpine.iso", "ubuntu.ISO"}


def test_listing_reports_size_and_mtime(settings: Settings):
    _make_iso(settings, "alpine.iso", size=4096)
    (iso,) = list_isos(settings)

    assert iso.size_bytes == 4096
    assert iso.modified_at.tzinfo is not None  # always timezone-aware


def test_listing_is_newest_first(settings: Settings):
    import os
    import time

    _make_iso(settings, "old.iso")
    time.sleep(0.01)
    _make_iso(settings, "new.iso")
    # Make the ordering unambiguous regardless of filesystem timestamp
    # granularity, which on Windows can be coarse enough to tie.
    os.utime(Path(settings.iso_dir) / "old.iso", (1_600_000_000, 1_600_000_000))

    assert [i.name for i in list_isos(settings)] == ["new.iso", "old.iso"]


# --------------------------------------------------------------------------- #
# Resolution — the security-relevant half
# --------------------------------------------------------------------------- #
def test_resolves_a_plain_filename(settings: Settings):
    made = _make_iso(settings, "alpine.iso")
    assert resolve_iso("alpine.iso", settings) == made.resolve()


def test_missing_iso_is_rejected(settings: Settings):
    with pytest.raises(IsoError, match="not found"):
        resolve_iso("ghost.iso", settings)


def test_non_iso_suffix_is_rejected(settings: Settings):
    (Path(settings.iso_dir) / "payload.txt").write_text("x")
    with pytest.raises(IsoError):
        resolve_iso("payload.txt", settings)


@pytest.mark.parametrize(
    "attack",
    [
        "../outside.iso",
        "../../outside.iso",
        "sub/../../outside.iso",
        "./../outside.iso",
    ],
)
def test_relative_traversal_is_rejected(settings: Settings, attack: str):
    """`../` must not reach a real ISO sitting just outside the directory."""
    outside = Path(settings.iso_dir).parent / "outside.iso"
    outside.write_bytes(b"\0" * 16)

    with pytest.raises(IsoError, match="outside the ISO directory"):
        resolve_iso(attack, settings)


def test_absolute_path_is_rejected(settings: Settings):
    outside = Path(settings.iso_dir).parent / "outside.iso"
    outside.write_bytes(b"\0" * 16)

    with pytest.raises(IsoError, match="outside the ISO directory"):
        resolve_iso(str(outside), settings)


def test_nested_subdirectory_is_rejected(settings: Settings):
    """Only the directory itself is served — no recursion, no nested escapes."""
    nested = Path(settings.iso_dir) / "sub"
    nested.mkdir()
    (nested / "deep.iso").write_bytes(b"\0" * 16)

    with pytest.raises(IsoError, match="outside the ISO directory"):
        resolve_iso("sub/deep.iso", settings)


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows path separators")
def test_windows_backslash_traversal_is_rejected(settings: Settings):
    outside = Path(settings.iso_dir).parent / "outside.iso"
    outside.write_bytes(b"\0" * 16)

    with pytest.raises(IsoError, match="outside the ISO directory"):
        resolve_iso("..\\outside.iso", settings)


def test_symlink_pointing_outside_is_rejected(settings: Settings, tmp_path: Path):
    """String checks would pass this; resolving the real path catches it."""
    outside = tmp_path / "outside.iso"
    outside.write_bytes(b"\0" * 16)
    link = Path(settings.iso_dir) / "innocent.iso"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")

    with pytest.raises(IsoError, match="outside the ISO directory"):
        resolve_iso("innocent.iso", settings)
