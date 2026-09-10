"""The installer bundles QEMU; something has to point at it.

This is the bug a real first install found, and the reason it survived every
check here is worth stating: the development machine has QEMU installed and on
PATH, so the bare names `qemu-system-x86_64` and `qemu-img` always resolved —
to the *system* copy. The bundled binaries were never the ones being run. On a
fresh machine there is nothing on PATH to find, the engine reports unavailable,
and 25 MB of working QEMU sits unused beside the executable.

So these tests never look at PATH. They assert the resolution rule itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kurukuru.config import Settings, bundled_qemu_binary


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    """An install layout: the executable, with QEMU in a `qemu` subdirectory."""
    (tmp_path / "qemu").mkdir()
    for name in ("qemu-system-x86_64.exe", "qemu-img.exe"):
        (tmp_path / "qemu" / name).write_bytes(b"MZ")
    return tmp_path


def test_a_bundled_binary_is_found_beside_the_executable(bundle):
    found = bundled_qemu_binary("qemu-system-x86_64", bundle)

    assert found is not None
    assert Path(found) == bundle / "qemu" / "qemu-system-x86_64.exe"
    assert Path(found).is_file()


def test_both_binaries_the_engine_needs_are_found(bundle):
    """is_available() runs *both*. Finding one and not the other still fails."""
    for name in ("qemu-system-x86_64", "qemu-img"):
        assert bundled_qemu_binary(name, bundle) is not None, name


def test_a_missing_bundle_resolves_to_nothing(tmp_path):
    """No bundle is a supported state — a checkout has none — and must fall
    back to PATH rather than inventing a path that does not exist."""
    assert bundled_qemu_binary("qemu-system-x86_64", tmp_path) is None


def test_a_directory_named_like_the_binary_is_not_mistaken_for_it(tmp_path):
    """`is_file()`, not `exists()`. A directory would resolve to a path that
    fails only later, when something tries to execute it."""
    (tmp_path / "qemu" / "qemu-img.exe").mkdir(parents=True)

    assert bundled_qemu_binary("qemu-img", tmp_path) is None


def test_an_unfrozen_checkout_keeps_using_path():
    """The default must not change for developers. Nothing here is frozen, so
    the settings should still hold the bare names PATH resolves."""
    assert not getattr(sys, "frozen", False), "test assumes an unfrozen run"

    settings = Settings()

    assert settings.qemu_system_binary == "qemu-system-x86_64"
    assert settings.qemu_img_binary == "qemu-img"


def test_an_explicit_setting_still_wins_over_the_bundle(bundle, monkeypatch):
    """An operator pointing at a specific build must not be overridden by
    whatever happens to sit beside the executable."""
    monkeypatch.setenv("KURUKURU_QEMU_SYSTEM_BINARY", r"D:\my-qemu\qemu-system-x86_64.exe")

    assert Settings().qemu_system_binary == r"D:\my-qemu\qemu-system-x86_64.exe"


def test_the_default_is_resolved_per_instance_not_captured_at_import(bundle, monkeypatch):
    """CONTRIBUTING rule 5, applied to the thing this fix just added.

    A module-level constant computed at import would be fixed for the life of
    the process and could not be redirected — which is how four earlier bugs in
    this project happened. The factory has to run when Settings is built.
    """
    monkeypatch.setenv("KURUKURU_QEMU_IMG_BINARY", "first")
    assert Settings().qemu_img_binary == "first"

    monkeypatch.setenv("KURUKURU_QEMU_IMG_BINARY", "second")
    assert Settings().qemu_img_binary == "second"
