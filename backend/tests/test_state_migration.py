"""
Moving a real install from ``~/.local-iaas`` to ``~/.kurukuru``.

Built the way ``test_migrations.py`` builds a pre-migration database: a real
old-layout tree on disk, with a real SQLite file, real ``runtime.json`` files
and — where ``qemu-img`` is available — real qcow2 overlays with real backing
files recorded in their headers. Then migrate it and assert that **every file
arrives and every persisted path is rewritten**, and that a second run does
nothing.

The refusals get as much attention as the happy path, because they are what
stands between an upgrade and a half-moved state tree.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from kurukuru.config import Settings
from kurukuru.product import DATABASE_LEAF, LEGACY_DATABASE_LEAF, apply_legacy_env
from kurukuru.state_migration import (
    MARKER_NAME,
    StateMigrationBlocked,
    migrate_state_dir,
    running_instances,
)

HAS_QEMU_IMG = shutil.which("qemu-img") is not None


# --------------------------------------------------------------------------- #
# Building an old-layout install
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE keypairs (
    id VARCHAR NOT NULL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    private_key_path VARCHAR
);
CREATE TABLE volumes (
    id VARCHAR NOT NULL PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    path VARCHAR NOT NULL
);
CREATE TABLE instances (
    id VARCHAR NOT NULL PRIMARY KEY,
    name VARCHAR(31) NOT NULL,
    iso VARCHAR
);
"""


def _runtime(
    *, iso_path: str | None = None, volumes: list[str] | None = None, pid=None
) -> dict:
    """A runtime file in the shape the QEMU engine actually writes."""
    return {
        "ssh_port": 2281,
        "qmp_port": 4400,
        "vnc_port": 5900,
        "pid": pid,
        "cpus": 2,
        "memory": "2048",
        "accel": "whpx",
        "iso_path": iso_path,
        "ssh_enabled": False,
        "display": "std",
        "volumes": volumes or [],
        "port_forwards": [],
        "guest_os": "windows",
    }


def build_legacy_install(root: Path, *, with_overlays: bool = False) -> Path:
    """A populated ``~/.local-iaas`` at ``root``. Returns it.

    Every directory a real install has, with real content in the ones whose
    content the migration has to understand.
    """
    root.mkdir(parents=True)
    for leaf in ("backups", "cloud-init", "isos", "keys"):
        (root / leaf).mkdir()
    (root / "qemu" / "base-images").mkdir(parents=True)
    (root / "qemu" / "instances").mkdir()
    (root / "qemu" / "volumes").mkdir()

    (root / "cli-token").write_text('{"token": "kurukuru_x", "api_url": "u"}', encoding="utf-8")
    (root / "isos" / "Windows10.iso").write_bytes(b"not really an iso")
    (root / "keys" / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
    (root / "keys" / "id_ed25519.pub").write_text("ssh-ed25519 AAA", encoding="utf-8")
    (root / "backups" / "iaas-20260801-120000-pre-migration.db").write_bytes(b"old backup")

    volume = root / "qemu" / "volumes" / "vol-1.qcow2"
    volume.write_bytes(b"volume")

    # Two instances: one ISO-booted with a volume attached, one plain.
    win = root / "qemu" / "instances" / "win10"
    win.mkdir()
    (win / "disk.qcow2").write_bytes(b"disk")
    (win / "qemu.log").write_text("serial output", encoding="utf-8")
    (win / "runtime.json").write_text(
        json.dumps(
            _runtime(
                iso_path=str(root / "isos" / "Windows10.iso"),
                volumes=[str(volume), "D:\\elsewhere\\extra.qcow2"],
            ),
            indent=2,
        ),
        encoding="utf-8",
    )

    plain = root / "qemu" / "instances" / "web-01"
    plain.mkdir()
    (plain / "runtime.json").write_text(json.dumps(_runtime(), indent=2), encoding="utf-8")

    database = root / LEGACY_DATABASE_LEAF
    conn = sqlite3.connect(database)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO keypairs VALUES (?, ?, ?)",
        ("k1", "orchestrator", str(root / "keys" / "id_ed25519")),
    )
    conn.execute(
        "INSERT INTO keypairs VALUES (?, ?, ?)", ("k2", "imported", None)
    )
    conn.execute("INSERT INTO volumes VALUES (?, ?, ?)", ("v1", "data", str(volume)))
    conn.execute(
        "INSERT INTO volumes VALUES (?, ?, ?)",
        ("v2", "external", "E:\\vm-storage\\big.qcow2"),
    )
    conn.execute("INSERT INTO instances VALUES (?, ?, ?)", ("i1", "win10", "Windows10.iso"))
    conn.commit()
    conn.close()

    if with_overlays:
        base = root / "qemu" / "base-images" / "noble.img"
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", str(base), "64M"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2",
             "-b", str(base), str(win / "disk.qcow2"), "64M"],
            check=True, capture_output=True,
        )
    return root


def _settings(target: Path) -> Settings:
    """Settings on the *default* layout, with the default pointed at ``target``.

    ``migrate_state_dir`` refuses to move anything when ``state_dir`` was set
    explicitly, so a test cannot simply pass a tmp path in: doing so exercises
    the refusal rather than the migration. The default itself is what has to
    move, so the default itself is what is patched.
    """
    settings = Settings(state_dir=str(target))
    # pydantic records the class default; the guard compares against it.
    Settings.model_fields["state_dir"].default = str(target)
    return settings


@pytest.fixture()
def install(tmp_path: Path) -> Iterator[tuple[Path, Path, Settings]]:
    """``(legacy, target, settings)`` for a populated old-layout install."""
    original = Settings.model_fields["state_dir"].default
    legacy = build_legacy_install(tmp_path / ".local-iaas")
    target = tmp_path / ".kurukuru"
    try:
        yield legacy, target, _settings(target)
    finally:
        Settings.model_fields["state_dir"].default = original


def _migrate(legacy: Path, target: Path, settings: Settings):
    return migrate_state_dir(settings, legacy_dir=str(legacy))


def _rows(database: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(database)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


def _runtime_of(target: Path, name: str) -> dict:
    return json.loads(
        (target / "qemu" / "instances" / name / "runtime.json").read_text(encoding="utf-8")
    )


# --------------------------------------------------------------------------- #
# Everything arrives
# --------------------------------------------------------------------------- #
def test_every_file_arrives(install):
    legacy, target, settings = install
    before = sorted(
        p.relative_to(legacy).as_posix() for p in legacy.rglob("*") if p.is_file()
    )

    report = _migrate(legacy, target, settings)

    assert report is not None
    assert not legacy.exists(), "the old directory must not be left behind"
    after = sorted(
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if p.is_file() and p.name != MARKER_NAME
    )

    # Two deliberate differences, and nothing else: the database is there under
    # its new name, and the pre-migration backup this run took is new. Mapping
    # them back is what turns "roughly the same files" into an exact assertion.
    assert DATABASE_LEAF in after and LEGACY_DATABASE_LEAF not in after
    fresh_backup = [
        name for name in after
        if name.startswith("backups/") and name not in before
    ]
    assert len(fresh_backup) == 1, fresh_backup

    normalised = sorted(
        LEGACY_DATABASE_LEAF if name == DATABASE_LEAF else name
        for name in after
        if name not in fresh_backup
    )
    assert normalised == before


def test_the_database_is_renamed_and_keeps_its_rows(install):
    legacy, target, settings = install
    report = _migrate(legacy, target, settings)

    assert report.database_renamed
    assert not (target / LEGACY_DATABASE_LEAF).exists()
    assert (target / DATABASE_LEAF).is_file()
    assert _rows(target / DATABASE_LEAF, "SELECT COUNT(*) FROM keypairs")[0][0] == 2


def test_the_write_ahead_log_travels_under_the_new_name(tmp_path):
    """A WAL must arrive as ``kurukuru.db-wal``, not left beside a gone name.

    A write-ahead log separated from its database is unreplayed committed
    transactions; one sitting beside the *wrong* database is a corrupt pair.
    """
    original = Settings.model_fields["state_dir"].default
    try:
        legacy = build_legacy_install(tmp_path / ".local-iaas")
        target = tmp_path / ".kurukuru"
        settings = _settings(target)

        # Leave a real WAL behind by committing in WAL mode without closing
        # cleanly enough to checkpoint it away.
        conn = sqlite3.connect(legacy / LEGACY_DATABASE_LEAF)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO instances VALUES ('i2', 'later', NULL)")
        conn.commit()
        conn.close()

        _migrate(legacy, target, settings)

        assert not any(target.glob(f"{LEGACY_DATABASE_LEAF}*"))
        assert _rows(
            target / DATABASE_LEAF, "SELECT name FROM instances WHERE id='i2'"
        ) == [("later",)]
    finally:
        Settings.model_fields["state_dir"].default = original


# --------------------------------------------------------------------------- #
# Persisted paths
# --------------------------------------------------------------------------- #
def test_runtime_files_are_repointed(install):
    legacy, target, settings = install
    report = _migrate(legacy, target, settings)

    runtime = _runtime_of(target, "win10")
    assert runtime["iso_path"] == str(target / "isos" / "Windows10.iso")
    assert runtime["volumes"][0] == str(target / "qemu" / "volumes" / "vol-1.qcow2")
    assert report.runtime_files_rewritten == 1  # web-01 had nothing to change


def test_a_path_outside_the_tree_is_left_alone(install):
    """An ISO or a volume the user keeps on another drive is not ours to move."""
    legacy, target, settings = install
    _migrate(legacy, target, settings)

    assert _runtime_of(target, "win10")["volumes"][1] == "D:\\elsewhere\\extra.qcow2"
    assert ("E:\\vm-storage\\big.qcow2",) in _rows(
        target / DATABASE_LEAF, "SELECT path FROM volumes"
    )


def test_volume_order_is_preserved(install):
    """The order is what the guest's /dev/vdb, /dev/vdc … follow.

    Reordering it renames the guest's disks, which breaks an ``/etc/fstab``
    written against the old names — so the rewrite is strictly element-wise.
    """
    legacy, target, settings = install
    before = _runtime_of(legacy, "win10")["volumes"]
    _migrate(legacy, target, settings)
    after = _runtime_of(target, "win10")["volumes"]

    assert len(after) == len(before)
    assert after[1] == before[1]                  # the untouched one stayed put
    assert after[0].endswith("vol-1.qcow2")       # and the moved one is still first


def test_database_paths_are_repointed(install):
    legacy, target, settings = install
    report = _migrate(legacy, target, settings)

    database = target / DATABASE_LEAF
    assert _rows(database, "SELECT private_key_path FROM keypairs WHERE id='k1'") == [
        (str(target / "keys" / "id_ed25519"),)
    ]
    assert _rows(database, "SELECT path FROM volumes WHERE id='v1'") == [
        (str(target / "qemu" / "volumes" / "vol-1.qcow2"),)
    ]
    assert report.database_rows_rewritten == {
        ("keypairs", "private_key_path"): 1,
        ("volumes", "path"): 1,
    }


def test_a_null_path_column_is_not_touched(install):
    legacy, target, settings = install
    _migrate(legacy, target, settings)

    assert _rows(
        target / DATABASE_LEAF, "SELECT private_key_path FROM keypairs WHERE id='k2'"
    ) == [(None,)]


@pytest.mark.skipif(not HAS_QEMU_IMG, reason="needs qemu-img on PATH")
def test_qcow2_backing_files_are_repointed(tmp_path):
    """The path nothing else records, and the one that breaks VMs silently.

    An instance disk is a copy-on-write overlay whose backing file is written
    *into the image header* as the absolute path it was created with. Neither
    the database nor ``runtime.json`` mentions it, so a migration that rewrote
    only those would move the tree, look entirely healthy, and fail every
    affected VM at launch with "Could not open backing file".
    """
    original = Settings.model_fields["state_dir"].default
    try:
        legacy = build_legacy_install(tmp_path / ".local-iaas", with_overlays=True)
        target = tmp_path / ".kurukuru"
        report = _migrate(legacy, target, _settings(target))

        overlay = target / "qemu" / "instances" / "win10" / "disk.qcow2"
        info = json.loads(
            subprocess.run(
                ["qemu-img", "info", "--output=json", str(overlay)],
                capture_output=True, text=True, check=True,
            ).stdout
        )
        assert info["backing-filename"] == str(target / "qemu" / "base-images" / "noble.img")
        assert not report.disks_needing_attention
        assert len(report.disks_rebased) == 1
        # And it actually opens, which is the thing the user cares about.
        subprocess.run(
            ["qemu-img", "check", str(overlay)], check=True, capture_output=True
        )
    finally:
        Settings.model_fields["state_dir"].default = original


# --------------------------------------------------------------------------- #
# The backup
# --------------------------------------------------------------------------- #
def test_a_backup_is_taken_before_anything_moves(install):
    legacy, target, settings = install
    report = _migrate(legacy, target, settings)

    assert report.backup is not None
    # Written into the tree *before* the move, so it travels with it rather than
    # being left at a path that is about to stop existing.
    assert report.backup.parent == legacy / "backups"
    landed = target / "backups" / report.backup.name
    assert landed.is_file()
    assert _rows(landed, "SELECT COUNT(*) FROM keypairs")[0][0] == 2


def test_a_pre_rename_backup_is_not_orphaned_by_the_new_naming(install):
    """The retention policy has to keep seeing backups it wrote under the old name."""
    legacy, target, settings = install
    _migrate(legacy, target, settings)

    assert (target / "backups" / "iaas-20260801-120000-pre-migration.db").is_file()


# --------------------------------------------------------------------------- #
# Idempotency
# --------------------------------------------------------------------------- #
def test_a_second_run_does_nothing(install):
    legacy, target, settings = install
    _migrate(legacy, target, settings)
    fingerprint = {
        p.relative_to(target).as_posix(): p.stat().st_size
        for p in sorted(target.rglob("*")) if p.is_file()
    }

    assert _migrate(legacy, target, settings) is None
    assert {
        p.relative_to(target).as_posix(): p.stat().st_size
        for p in sorted(target.rglob("*")) if p.is_file()
    } == fingerprint


def test_the_marker_records_what_happened(install):
    legacy, target, settings = install
    report = _migrate(legacy, target, settings)

    marker = json.loads((target / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["from"] == str(legacy)
    assert marker["to"] == str(target)
    assert marker["database_renamed"] is True
    assert marker["database_rows_rewritten"] == {
        "keypairs.private_key_path": 1, "volumes.path": 1
    }
    assert marker["database_backup"] == str(report.backup)


# --------------------------------------------------------------------------- #
# The refusals
# --------------------------------------------------------------------------- #
@pytest.fixture()
def holds_a_port() -> Iterator[tuple[int, int]]:
    """``(pid, port)`` of a live process holding a loopback port.

    Stands in for a running QEMU: the migration's liveness test is "the pid is
    alive *and* its QMP port is still bound", and both halves have to be real
    for the test to mean anything.
    """
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    script = textwrap.dedent(
        f"""
        import socket, time
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", {port}))
        s.listen(1)
        time.sleep(120)
        """
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = 200
        while deadline:
            with socket.socket() as check:
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    break
            deadline -= 1
        yield process.pid, port
    finally:
        process.kill()
        process.wait()


def _mark_running(legacy: Path, name: str, pid: int, port: int) -> None:
    path = legacy / "qemu" / "instances" / name / "runtime.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pid"] = pid
    data["qmp_port"] = port
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_a_running_instance_refuses_the_whole_migration(install, holds_a_port):
    """Nothing is moved, and the message says which VMs and what to do.

    Refusing loudly beats starting: a backend that came up anyway would open a
    fresh empty database at the new path while the real one sat untouched at the
    old one, and present as an install that had lost everything.
    """
    legacy, target, settings = install
    pid, port = holds_a_port
    _mark_running(legacy, "win10", pid, port)

    with pytest.raises(StateMigrationBlocked) as blocked:
        _migrate(legacy, target, settings)

    assert "win10" in str(blocked.value)
    assert str(pid) in str(blocked.value)
    assert "Nothing has been moved" in str(blocked.value)
    assert legacy.is_dir() and (legacy / LEGACY_DATABASE_LEAF).is_file()
    assert not target.exists()


def test_a_recycled_pid_does_not_block_the_migration(install, holds_a_port):
    """A live pid whose QMP port is free is somebody else's process, not a VM.

    The two signals are kept apart for exactly this: refusing an upgrade because
    an unrelated program inherited an old pid number is a failure the user
    cannot diagnose and cannot work around.
    """
    legacy, target, settings = install
    pid, _port = holds_a_port

    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    unbound = free.getsockname()[1]
    free.close()
    _mark_running(legacy, "win10", pid, unbound)

    assert running_instances(legacy) == []
    assert _migrate(legacy, target, settings) is not None


def test_an_explicitly_configured_state_dir_is_left_alone(tmp_path, caplog):
    """A location somebody chose is a decision, not something to migrate over."""
    legacy = build_legacy_install(tmp_path / ".local-iaas")
    chosen = tmp_path / "chosen"
    settings = Settings(state_dir=str(chosen))  # default untouched: explicit

    assert migrate_state_dir(settings, legacy_dir=str(legacy)) is None
    assert legacy.is_dir()
    assert not chosen.exists()


def test_a_target_that_already_has_contents_is_never_written_over(install):
    """Two state trees is a question for a human, not one to resolve by picking."""
    legacy, target, settings = install
    target.mkdir()
    (target / DATABASE_LEAF).write_bytes(b"someone else's install")

    assert _migrate(legacy, target, settings) is None
    assert (target / DATABASE_LEAF).read_bytes() == b"someone else's install"
    assert (legacy / LEGACY_DATABASE_LEAF).is_file()


def test_an_engine_pointing_somewhere_else_stops_the_scan(install):
    """The second condition, and what keeps this away from the test suite.

    Every test runs against an engine of its own. A tree-moving migration
    guarded by one condition would be one careless fixture away from relocating
    a developer's real install into a ``tmp_path``.
    """
    from sqlmodel import create_engine

    legacy, target, settings = install
    elsewhere = create_engine(f"sqlite:///{(target.parent / 'other.db').as_posix()}")
    try:
        assert migrate_state_dir(settings, elsewhere, legacy_dir=str(legacy)) is None
        assert legacy.is_dir()
    finally:
        elsewhere.dispose()


def test_a_sibling_sharing_a_name_prefix_is_not_swept_in(install):
    """``.local-iaas`` and ``.local-iaas-backup`` are different directories.

    Compared by path component rather than string prefix, because a prefix match
    would silently repoint a path the user deliberately kept outside the tree.
    """
    legacy, target, settings = install
    sibling = f"{legacy}-backup"
    path = legacy / "qemu" / "instances" / "web-01" / "runtime.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["iso_path"] = os.path.join(sibling, "isos", "kept.iso")
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    _migrate(legacy, target, settings)

    assert _runtime_of(target, "web-01")["iso_path"] == os.path.join(
        sibling, "isos", "kept.iso"
    )


def test_nothing_happens_without_a_legacy_directory(tmp_path):
    original = Settings.model_fields["state_dir"].default
    try:
        target = tmp_path / ".kurukuru"
        assert migrate_state_dir(
            _settings(target), legacy_dir=str(tmp_path / "absent")
        ) is None
    finally:
        Settings.model_fields["state_dir"].default = original


# --------------------------------------------------------------------------- #
# The environment-variable deprecation
# --------------------------------------------------------------------------- #
@pytest.fixture()
def clean_env() -> Iterator[None]:
    """Restore ``os.environ`` wholesale afterwards.

    ``apply_legacy_env`` works by *writing into the environment* — that is how
    the value reaches ``pydantic-settings``, which reads ``os.environ`` and not
    anything a test can hand it. ``monkeypatch`` only undoes what monkeypatch
    itself set, so the ``KURUKURU_*`` name the shim creates would survive
    teardown and be inherited by every test that ran afterwards. It did: the
    first version of these tests left ``KURUKURU_QEMU_DIR=D:/vm-disks`` behind
    and twenty-eight volume tests failed on a drive that does not exist.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def test_a_legacy_variable_is_honoured_and_reported(clean_env):
    os.environ.pop("KURUKURU_STATE_DIR", None)
    os.environ["IAAS_STATE_DIR"] = "D:/vms"

    assert ("IAAS_STATE_DIR", "KURUKURU_STATE_DIR") in apply_legacy_env()
    assert os.environ["KURUKURU_STATE_DIR"] == "D:/vms"


def test_the_new_name_wins_when_both_are_set(clean_env):
    """Someone part-way through the rename meant the one they just wrote."""
    os.environ["IAAS_STATE_DIR"] = "D:/old"
    os.environ["KURUKURU_STATE_DIR"] = "D:/new"

    assert ("IAAS_STATE_DIR", "KURUKURU_STATE_DIR") not in apply_legacy_env()
    assert os.environ["KURUKURU_STATE_DIR"] == "D:/new"


def test_a_legacy_variable_reaches_the_settings_it_names(clean_env):
    """The whole point: an operator's configured path is not sent to a default.

    Composed rather than routed through ``get_settings``, which the isolation
    fixture has already replaced with one bound to this test's ``tmp_path`` —
    what is being checked is that the shim and ``Settings`` agree, and building
    both here says so without depending on which of them the fixture owns.
    """
    os.environ.pop("KURUKURU_QEMU_DIR", None)
    os.environ["IAAS_QEMU_DIR"] = "D:/vm-disks"

    apply_legacy_env()

    assert Settings().qemu_dir == "D:/vm-disks"


def test_the_shim_leaves_an_environment_without_legacy_names_alone(clean_env):
    for name in [key for key in os.environ if key.startswith("IAAS_")]:
        del os.environ[name]
    before = dict(os.environ)

    assert apply_legacy_env() == []
    assert os.environ == before


# --------------------------------------------------------------------------- #
# A rename that fails for a reason other than "different volume"
# --------------------------------------------------------------------------- #
def test_an_open_file_refuses_rather_than_copying(install, monkeypatch):
    """The failure that is *likely*, and the one a copy would make worse.

    Windows refuses to rename a directory containing an open file, so a running
    VM or a still-serving older backend lands here. Falling back to
    ``shutil.move`` would answer that by copying twenty gigabytes around an open
    disk image — slow, and a torn image at the end of it. So it refuses, and
    says what to look for.
    """
    legacy, target, settings = install

    def refuse(self, _destination):  # noqa: ANN001
        raise PermissionError(13, "The process cannot access the file", None, 32)

    monkeypatch.setattr(Path, "rename", refuse)

    with pytest.raises(StateMigrationBlocked) as blocked:
        _migrate(legacy, target, settings)

    assert "open" in str(blocked.value)
    assert legacy.is_dir()
    assert not target.exists()


def test_a_cross_device_rename_falls_back_to_copying(install, monkeypatch):
    """The one case that has no atomic form, on any platform."""
    legacy, target, settings = install
    real_rename = Path.rename
    calls = {"n": 0}

    def only_the_tree_fails(self, destination):  # noqa: ANN001
        if str(self) == str(legacy):
            calls["n"] += 1
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(self, destination)

    monkeypatch.setattr(Path, "rename", only_the_tree_fails)

    report = _migrate(legacy, target, settings)

    assert calls["n"] == 1
    assert report is not None
    assert not legacy.exists()
    assert (target / DATABASE_LEAF).is_file()
    assert _runtime_of(target, "win10")["iso_path"] == str(target / "isos" / "Windows10.iso")


def test_a_failed_cross_device_copy_leaves_no_partial_target(install, monkeypatch):
    """A partial copy left behind would lock the user out of retrying.

    The next start would find a target with contents in it and refuse to move
    onto it — correctly, by its own rule — so the wreckage of a failed attempt
    has to be cleared by the attempt that made it.
    """
    legacy, target, settings = install

    def cross_device(self, _destination):  # noqa: ANN001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def half_a_copy(source, destination):  # noqa: ANN001
        Path(destination).mkdir(parents=True, exist_ok=True)
        (Path(destination) / "partial").write_bytes(b"half")
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "rename", cross_device)
    monkeypatch.setattr(shutil, "move", half_a_copy)

    with pytest.raises(StateMigrationBlocked) as blocked:
        _migrate(legacy, target, settings)

    assert "partial copy has been removed" in str(blocked.value)
    assert not target.exists()
    assert (legacy / LEGACY_DATABASE_LEAF).is_file()
