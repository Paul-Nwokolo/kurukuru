"""Removing a Linux install's data: trash or rename aside, never delete.

Every test ends by checking the bytes still exist somewhere — in the fake
trash or beside the original. A removal path that could delete is the one
this module exists to rule out (DECISIONS #63, #72).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from kurukuru import state_removal as sr


@pytest.fixture()
def state(tmp_path: Path) -> Path:
    root = tmp_path / "kurukuru"
    files = {
        "qemu/instances/a/disk.qcow2": 4000,
        "qemu/base-images/noble.img": 2000,
        "isos/alpine.iso": 1000,
        "backups/kurukuru-1-pre-migration.db": 300,
        "keys/id_ed25519": 100,
        "kurukuru.db": 200,
        "kurukuru.db-wal": 50,
        "cli-token": 10,
    }
    for rel, size in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"x" * size)
    return root


def test_the_inventory_says_what_is_there_and_how_big(state):
    rows = dict(sr.inventory(state))
    assert rows == {
        "VM disks": 4000, "Images": 2000, "Boot media (ISOs)": 1000,
        "Database backups": 300, "SSH keys": 100, "Database": 250, "Other": 10,
    }
    assert sum(rows.values()) == 7660


def test_with_no_trash_the_tree_is_renamed_aside(state):
    done = sr.remove(state, which=lambda name: None,
                     now=datetime(2026, 10, 7, 15, 30, 0))

    assert done.method == "moved aside"
    assert done.destination == state.with_name("kurukuru.removed-20261007-153000")
    assert not state.exists()
    assert (done.destination / "kurukuru.db").read_bytes() == b"x" * 200


def test_a_second_removal_the_same_second_does_not_collide(state, tmp_path):
    now = datetime(2026, 10, 7, 15, 30, 0)
    first = sr.remove(state, which=lambda name: None, now=now)
    state.mkdir()
    (state / "kurukuru.db").write_bytes(b"second")
    second = sr.remove(state, which=lambda name: None, now=now)

    assert first.destination != second.destination
    assert (second.destination / "kurukuru.db").read_bytes() == b"second"


def _fake_trash(trash_dir: Path, *, succeed: bool = True):
    def runner(command, **kwargs):
        assert kwargs.get("timeout")
        if succeed:
            shutil.move(command[-1], trash_dir / Path(command[-1]).name)
        return subprocess.CompletedProcess(command, 0 if succeed else 1, "", "")
    return runner


def test_gio_trash_is_preferred_when_present(state, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    done = sr.remove(state, which=lambda name: "/usr/bin/gio" if name == "gio" else None,
                     runner=_fake_trash(trash))

    assert done.method == "gio trash"
    assert (trash / "kurukuru" / "kurukuru.db").exists()


def test_trash_cli_is_the_fallback(state, tmp_path):
    trash = tmp_path / "Trash"
    trash.mkdir()
    done = sr.remove(state, which=lambda name: "/usr/bin/trash-put" if name == "trash-put" else None,
                     runner=_fake_trash(trash))
    assert done.method == "trash-put"


def test_a_trash_that_fails_falls_through_to_the_rename_not_to_a_delete(state, tmp_path):
    done = sr.remove(state, which=lambda name: f"/usr/bin/{name}",
                     runner=_fake_trash(tmp_path, succeed=False))

    assert done.method == "moved aside"
    assert (done.destination / "keys" / "id_ed25519").exists()


def test_the_module_cannot_delete():
    """A guard on the source itself: no rmtree, no unlink, no os.remove."""
    source = Path(sr.__file__).read_text(encoding="utf-8")
    for call in ("rmtree(", ".unlink(", "os.remove(", "os.rmdir("):
        assert call not in source, f"state_removal must not call {call}"
