"""Pre-migration database backups.

The guarantee under test is narrow and load-bearing: *if a startup is about to
change the schema, a restorable copy of the database exists first, and if it is
not about to change anything, nothing is written.* The second half matters as
much as the first — a backup taken on every start fills the retention window
with identical copies and ages the one useful restore point out of it.

Nothing here copies files by hand. The point of the online backup API is that it
produces a single self-contained ``.db`` with the WAL folded in, so every test
opens the backup with a fresh sqlite3 connection and reads rows out of it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlmodel import create_engine

import app.database as db_module
from app.config import Settings

_LEGACY_SCHEMA = """
CREATE TABLE instances (
    name VARCHAR(31) NOT NULL,
    flavor VARCHAR(6) NOT NULL,
    id VARCHAR NOT NULL,
    status VARCHAR(12) NOT NULL,
    ip_address VARCHAR,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    error_message VARCHAR,
    PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_instances_name ON instances (name);
"""


def _row(n: int) -> tuple:
    return (f"vm-{n}", "SMALL", f"id-{n}", "RUNNING", None,
            "2026-08-01 10:00:00", "2026-08-01 10:00:00", None)


def _count(path: Path, table: str = "instances") -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _names(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM instances")}
    finally:
        conn.close()


@pytest.fixture()
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict]:
    """A pre-migration database with rows, wired into app.database.

    Settings are patched too, and must agree with the engine: ``backup_database``
    refuses to run when they disagree, which is what stops the suite writing
    copies of a tmp_path database into a developer's real state directory.
    """
    db_path = tmp_path / "iaas.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO instances (name, flavor, id, status, ip_address, "
        "created_at, updated_at, error_message) VALUES (?,?,?,?,?,?,?,?)",
        [_row(n) for n in range(3)],
    )
    conn.commit()
    conn.close()

    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, connect_args={"check_same_thread": False})
    settings = Settings(state_dir=str(tmp_path), database_url=url)

    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(db_module, "get_settings", lambda: settings)

    yield {"db": db_path, "settings": settings,
           "backups": Path(settings.db_backup_dir), "engine": engine}
    engine.dispose()


def _backups(directory: Path) -> list[Path]:
    """Every automatic backup in a directory, under either naming.

    Both prefixes, because Phase 16 renamed the product and a real backups
    directory now holds files from both sides of that rename — the pre-Phase-16
    ones are still this module's own, and a retention policy that stopped seeing
    them would keep them forever while pruning around them.
    """
    if not directory.exists():
        return []
    return sorted(
        {path for glob in db_module._BACKUP_GLOBS for path in directory.glob(glob)},
        key=db_module._backup_stamp,
    )


# --------------------------------------------------------------------------- #
# The backup itself
# --------------------------------------------------------------------------- #
def test_a_migration_leaves_a_backup_with_every_row_in_it(legacy):
    db_module.init_db()

    made = _backups(legacy["backups"])
    assert len(made) == 1, "a schema change must leave exactly one restore point"

    backup = made[0]
    # It opens as a database, not just as bytes on disk.
    assert _count(backup) == 3
    assert _names(backup) == {"vm-0", "vm-1", "vm-2"}
    # And it is the *pre*-migration shape: the point of a restore point is that
    # it is what you had before, not a copy of the result.
    conn = sqlite3.connect(backup)
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(instances)")}
    finally:
        conn.close()
    assert "guest_os" not in columns


def test_the_backup_needs_no_sidecars(legacy):
    """The online backup API folds the WAL in, which is the whole reason for
    preferring it over copying files. A restore that needed a matching -wal
    would be a second thing to get right at the worst possible moment."""
    db_module.init_db()
    backup = _backups(legacy["backups"])[0]

    assert not Path(f"{backup}-wal").exists()
    assert not Path(f"{backup}-shm").exists()
    # Readable entirely on its own, copied somewhere else with nothing beside it.
    alone = legacy["db"].parent / "moved-elsewhere.db"
    alone.write_bytes(backup.read_bytes())
    assert _count(alone) == 3


def test_a_converged_startup_writes_nothing(legacy):
    """The second start has nothing to add or redefine, so it must not leave a
    copy — otherwise ordinary restarts evict the useful restore point."""
    db_module.init_db()
    assert len(_backups(legacy["backups"])) == 1

    db_module.init_db()
    db_module.init_db()
    assert len(_backups(legacy["backups"])) == 1, "no-op startups must not back up"


def test_pending_is_empty_once_converged(legacy):
    from sqlalchemy import inspect

    db_module.init_db()
    inspector = inspect(legacy["engine"])
    pending = db_module._pending_migrations(inspector, set(inspector.get_table_names()))

    assert not pending
    assert pending.describe() == "nothing"


def test_a_backup_is_refused_when_engine_and_settings_disagree(legacy, tmp_path):
    """The guard that keeps the suite out of a real state directory."""
    elsewhere = Settings(state_dir=str(tmp_path / "other"),
                         database_url="sqlite:///C:/somewhere/else/iaas.db")
    assert db_module.backup_database(elsewhere, legacy["engine"]) is None
    assert _backups(Path(elsewhere.db_backup_dir)) == []


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_retention_keeps_the_newest_and_prunes_the_rest(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    for day in range(1, 9):
        (directory / f"iaas-2026080{day}-120000-pre-migration.db").write_bytes(b"x")

    removed = db_module._prune_backups(directory, keep=5)

    kept = sorted(p.name for p in directory.glob("*.db"))
    assert len(kept) == 5
    assert kept[0] == "iaas-20260804-120000-pre-migration.db"   # oldest survivor
    assert kept[-1] == "iaas-20260808-120000-pre-migration.db"  # newest
    assert len(removed) == 3


def test_retention_never_touches_anything_it_did_not_write(tmp_path):
    """The directory also holds hand-made backups — Phase 13 left one, as a
    directory with its own name. Tidying up someone else's copy would be a
    much worse bug than keeping one file too many."""
    directory = tmp_path / "backups"
    directory.mkdir()
    for day in range(1, 8):
        (directory / f"iaas-2026080{day}-120000-pre-migration.db").write_bytes(b"x")
    manual_dir = directory / "iaas-20260814-170650"
    manual_dir.mkdir()
    (manual_dir / "iaas-20260814-170650.db").write_bytes(b"manual")
    stray = directory / "notes.txt"
    stray.write_text("keep me")

    db_module._prune_backups(directory, keep=2)

    assert manual_dir.is_dir() and (manual_dir / "iaas-20260814-170650.db").exists()
    assert stray.exists()
    assert len(_backups(directory)) == 2


def test_retention_of_zero_disables_pruning(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir()
    for day in range(1, 5):
        (directory / f"iaas-2026080{day}-120000-pre-migration.db").write_bytes(b"x")

    assert db_module._prune_backups(directory, keep=0) == []
    assert len(_backups(directory)) == 4


def test_retention_is_applied_end_to_end(legacy, monkeypatch):
    """Retention runs as part of a real migration, not only when called directly."""
    monkeypatch.setattr(legacy["settings"], "db_backup_retention", 2)
    directory = legacy["backups"]
    directory.mkdir(parents=True, exist_ok=True)
    for day in range(1, 5):
        (directory / f"iaas-2026080{day}-120000-pre-migration.db").write_bytes(b"x")

    db_module.init_db()

    remaining = _backups(directory)
    assert len(remaining) == 2
    # The newest is the one just taken, so the oldest fakes went first.
    assert not (directory / "iaas-20260801-120000-pre-migration.db").exists()


def test_two_backups_in_the_same_second_do_not_collide(legacy):
    directory = legacy["backups"]
    directory.mkdir(parents=True, exist_ok=True)
    first = db_module.backup_database(legacy["settings"], legacy["engine"])
    second = db_module.backup_database(legacy["settings"], legacy["engine"])

    assert first is not None and second is not None
    assert first != second
    assert _count(first) == _count(second) == 3
