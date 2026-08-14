"""
Schema migration tests.

``init_db`` has to bring a database created by an *older* build up to the
current schema, because ``create_all`` only ever creates missing tables and
indexes — it silently leaves an existing one alone no matter how its definition
has since changed. These tests build a pre-migration database by hand and assert
that ``init_db`` converges it, twice (idempotence), without losing rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlmodel import Session, create_engine, select

import app.database as db_module
from app.models import BootSource, Instance, InstanceEvent, InstanceStatus, Project

# The `instances` table exactly as the pre-Phase-5 build created it: no engine
# or runtime-port columns, and a UNIQUE index on name.
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
CREATE INDEX ix_instances_status ON instances (status);
"""

# SQLAlchemy's Enum column persists the enum *name* ("SMALL"), not its value
# ("small") — matching what a real iaas.db contains.
_LEGACY_ROW = (
    "legacy-vm", "SMALL", "id-legacy", "TERMINATED",
    None, "2026-08-01 10:00:00", "2026-08-01 10:00:00", None,
)


@pytest.fixture()
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A database in the pre-migration shape, wired into app.database."""
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO instances "
        "(name, flavor, id, status, ip_address, created_at, updated_at, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        _LEGACY_ROW,
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)
    yield path
    engine.dispose()


def _indexes(path: Path) -> dict[str, bool]:
    """Map index name -> is_unique, straight from SQLite."""
    conn = sqlite3.connect(path)
    try:
        return {row[1]: bool(row[2]) for row in conn.execute("PRAGMA index_list(instances)")}
    finally:
        conn.close()


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(instances)")}
    finally:
        conn.close()


def test_legacy_db_starts_with_a_unique_name_index(legacy_db: Path):
    """Guards the fixture itself — otherwise the migration test proves nothing."""
    assert _indexes(legacy_db)["ix_instances_name"] is True


def test_init_db_drops_the_unique_name_index(legacy_db: Path):
    db_module.init_db()

    indexes = _indexes(legacy_db)
    # The index must survive (reconciliation looks rows up by name) but lose
    # its uniqueness, which is what permanently reserved terminated names.
    assert "ix_instances_name" in indexes
    assert indexes["ix_instances_name"] is False


def test_init_db_adds_the_phase5_columns(legacy_db: Path):
    db_module.init_db()

    assert {"engine", "ssh_port", "vnc_port", "qmp_port", "pid"} <= _columns(legacy_db)


def test_init_db_adds_the_phase6_columns(legacy_db: Path):
    db_module.init_db()

    assert {"boot_source", "accel", "iso", "image_id"} <= _columns(legacy_db)


def test_init_db_adds_the_phase7_display_column(legacy_db: Path):
    db_module.init_db()
    assert "display" in _columns(legacy_db)


def test_legacy_rows_have_no_display_recorded(legacy_db: Path):
    """Pre-Phase-7 rows all ran std VGA; the column reads null and the caveat
    logic treats that as std rather than guessing."""
    db_module.init_db()
    with Session(db_module.engine) as session:
        assert session.exec(select(Instance)).one().display is None


def test_init_db_backfills_sizing_from_the_preset_label(legacy_db: Path):
    """Sizing moved from a preset *reference* to numbers on the row.

    The catalog is consulted once, during migration; afterwards the row stands
    on its own, so editing a preset later cannot silently change what an
    existing instance claims to be.
    """
    db_module.init_db()

    with Session(db_module.engine) as session:
        row = session.exec(select(Instance)).one()

    # The legacy fixture row is flavor=SMALL.
    assert (row.cpus, row.memory_mb, row.disk_gb) == (1, 1024, 5)
    assert row.flavor == "SMALL"  # label preserved verbatim, not rewritten


def test_backfill_leaves_an_unknown_preset_null(legacy_db: Path, monkeypatch):
    """A label for a preset since renamed or removed. Inventing a size would
    misreport history, so the numbers stay null and the row says so."""
    import sqlite3

    conn = sqlite3.connect(legacy_db)
    conn.execute(
        "INSERT INTO instances (name, flavor, id, status, created_at, updated_at) "
        "VALUES ('ghost-preset', 'gargantuan', 'id-ghost', 'TERMINATED', "
        "'2026-08-01 10:00:00', '2026-08-01 10:00:00')"
    )
    conn.commit()
    conn.close()

    db_module.init_db()

    with Session(db_module.engine) as session:
        row = session.exec(select(Instance).where(Instance.name == "ghost-preset")).one()
    assert row.cpus is None and row.memory_mb is None


def test_init_db_creates_the_images_table(legacy_db: Path):
    """A brand-new table alongside existing ones — create_all's job, but only
    if the model is imported before it runs."""
    db_module.init_db()

    assert "images" in inspect(db_module.engine).get_table_names()


def test_init_db_creates_the_events_table_without_touching_existing_rows(legacy_db: Path):
    """Phase 11's table. New tables are create_all's job — the risk is only
    that a model added to models.py is never imported, in which case create_all
    silently does nothing and every query against it fails at runtime.

    The row assertion is the other half: an install that has been running since
    Phase 1 gains an empty history, not a rewritten one. Events are recorded
    from this point forward; nothing back-dates them, because inventing history
    is worse than not having it.
    """
    db_module.init_db()

    assert "instance_events" in inspect(db_module.engine).get_table_names()
    with Session(db_module.engine) as session:
        assert session.exec(select(InstanceEvent)).all() == []
        assert len(session.exec(select(Instance)).all()) == 1


def test_legacy_rows_default_to_image_boot_source(legacy_db: Path):
    """Every pre-Phase-6 instance came from a cloud image, by definition."""
    db_module.init_db()

    with Session(db_module.engine) as session:
        row = session.exec(select(Instance)).one()

    assert row.boot_source is BootSource.IMAGE
    assert row.iso is None
    assert row.image_id is None
    assert row.accel is None


def test_migration_preserves_existing_rows_and_backfills_engine(legacy_db: Path):
    db_module.init_db()

    with Session(db_module.engine) as session:
        rows = session.exec(select(Instance)).all()

    assert len(rows) == 1
    assert rows[0].name == "legacy-vm"
    assert rows[0].status is InstanceStatus.TERMINATED
    # Pre-Phase-5 rows predate the engine column; they must read as Multipass.
    assert rows[0].engine == "multipass"
    assert rows[0].ssh_port is None


def test_duplicate_names_are_accepted_after_migration(legacy_db: Path):
    """The point of the whole change: the terminated 'legacy-vm' name is free."""
    db_module.init_db()

    with Session(db_module.engine) as session:
        session.add(Instance(name="legacy-vm", status=InstanceStatus.RUNNING))
        session.commit()  # would raise IntegrityError against the unique index

        names = session.exec(select(Instance).where(Instance.name == "legacy-vm")).all()
    assert len(names) == 2


def test_init_db_is_idempotent(legacy_db: Path):
    db_module.init_db()
    before = (_indexes(legacy_db), _columns(legacy_db))

    db_module.init_db()  # second run must be a no-op, not an error
    assert (_indexes(legacy_db), _columns(legacy_db)) == before


def test_init_db_on_a_fresh_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A brand-new DB must land in the same shape as a migrated one."""
    path = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    monkeypatch.setattr(db_module, "engine", engine)

    db_module.init_db()

    assert _indexes(path)["ix_instances_name"] is False
    assert {"engine", "ssh_port", "vnc_port", "qmp_port", "pid"} <= _columns(path)
    assert "instances" in inspect(engine).get_table_names()
    engine.dispose()


def test_init_db_seeds_a_default_project_and_adopts_existing_rows(legacy_db: Path):
    """The migration an existing install actually feels.

    project_id is nullable and the API reads null as "the default project", so
    an install that skipped the backfill would still *work* — but selecting a
    project asks for rows whose project_id matches, and every pre-existing
    instance would match nothing and vanish from the filtered view. "My VMs
    disappeared after upgrading" is the failure this prevents.
    """
    db_module.init_db()

    with Session(db_module.engine) as session:
        projects = session.exec(select(Project)).all()
        instances = session.exec(select(Instance)).all()

    assert len(projects) == 1
    assert projects[0].is_default is True
    assert len(instances) == 1
    assert instances[0].name == "legacy-vm"
    assert instances[0].project_id == projects[0].id


def test_the_backfill_does_not_move_a_row_that_was_already_filed(legacy_db: Path):
    """Idempotence that matters: init_db runs on every start, and a second pass
    must not drag a deliberately filed resource back to the default."""
    db_module.init_db()

    with Session(db_module.engine) as session:
        elsewhere = Project(name="client-a")
        session.add(elsewhere)
        session.commit()
        session.refresh(elsewhere)
        row = session.exec(select(Instance)).one()
        row.project_id = elsewhere.id
        session.add(row)
        session.commit()
        moved_to = elsewhere.id

    db_module.init_db()  # a restart

    with Session(db_module.engine) as session:
        assert session.exec(select(Instance)).one().project_id == moved_to


def test_seeding_the_default_project_is_idempotent(legacy_db: Path):
    db_module.init_db()
    db_module.init_db()

    with Session(db_module.engine) as session:
        assert len(session.exec(select(Project).where(Project.is_default)).all()) == 1


# --------------------------------------------------------------------------- #
# Relocating a database written by the CWD-relative default
# --------------------------------------------------------------------------- #
"""
The database moved from ``./iaas.db`` — wherever the backend happened to be
started from — to ``<state_dir>/iaas.db``. Every install that predates that has
its rows in the old place, and a backend that quietly opened the new path would
present as an install that had lost everything.

These cover the move itself and, more importantly, the three refusals. A
migration that moves files is the one kind that can destroy what it was meant
to rescue.
"""


def _make_db(path: Path, marker: str = "kept") -> Path:
    """A small but genuinely non-empty SQLite database."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE relic (note TEXT)")
        conn.execute("INSERT INTO relic VALUES (?)", (marker,))
        conn.commit()
    finally:
        conn.close()
    return path


def _read_marker(path: Path) -> str | None:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT note FROM relic").fetchone()[0]
    except sqlite3.DatabaseError:
        return None
    finally:
        conn.close()


@pytest.fixture()
def relocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A default-layout install whose legacy database sits in ``old/``.

    Returns ``(run, legacy, target)``. ``run`` performs the relocation with
    settings and an engine that agree, which is the only configuration under
    which it does anything at all.
    """
    from app.config import Settings
    from sqlalchemy import create_engine as sa_create_engine

    state = tmp_path / "state"
    legacy = tmp_path / "old" / "iaas.db"
    settings = Settings(state_dir=str(state))
    target = settings.database_path
    assert target == state / "iaas.db"

    monkeypatch.setattr(db_module, "_legacy_database_candidates", lambda: [legacy])
    engine = sa_create_engine(f"sqlite:///{target.as_posix()}")

    def run() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        db_module.relocate_legacy_database(settings, engine)

    yield run, legacy, target
    engine.dispose()


def test_an_existing_database_is_moved_with_its_rows(relocation):
    run, legacy, target = relocation
    _make_db(legacy, "the real history")

    run()

    assert not legacy.exists()
    assert _read_marker(target) == "the real history"


def test_the_write_ahead_log_travels_with_the_database(relocation):
    """A WAL is committed transactions that have not been folded in yet.

    Left behind, they are lost; moved next to the *wrong* database, the pair is
    corrupt. Either way the user's last few actions are the ones that vanish,
    which is the worst possible slice to lose.
    """
    run, legacy, target = relocation
    _make_db(legacy)
    Path(f"{legacy}-wal").write_bytes(b"pending transactions")
    Path(f"{legacy}-shm").write_bytes(b"shared memory index")

    run()

    assert Path(f"{target}-wal").read_bytes() == b"pending transactions"
    assert Path(f"{target}-shm").read_bytes() == b"shared memory index"
    assert not Path(f"{legacy}-wal").exists()
    assert not Path(f"{legacy}-shm").exists()


def test_a_target_that_already_has_rows_is_never_written_over(relocation):
    """Two databases with history in them is a question for a human.

    Picking one silently is how a migration destroys the thing it exists to
    protect, so both are left exactly where they are and the log names them.
    """
    run, legacy, target = relocation
    _make_db(legacy, "old")
    _make_db(target, "new")

    run()

    assert _read_marker(legacy) == "old"
    assert _read_marker(target) == "new"


def test_a_zero_byte_target_does_not_block_the_move(relocation):
    """The stray this whole change is cleaning up.

    Merely pointing SQLite at a path creates an empty file there, so a single
    start from the wrong directory left a 0-byte ``iaas.db`` sitting in the new
    location. That is not data, and treating it as data would strand the real
    database forever.
    """
    run, legacy, target = relocation
    _make_db(legacy, "the real history")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    assert target.stat().st_size == 0

    run()

    assert _read_marker(target) == "the real history"


def test_nothing_happens_when_the_database_was_configured_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Naming a path is a decision, and it is also what every test does.

    This is the condition that keeps a file-moving migration away from the
    suite: with ``database_url`` set to somewhere of its own, the scan never
    starts, so no fixture can end up dragging a developer's real database into
    a ``tmp_path``.
    """
    from app.config import Settings
    from sqlalchemy import create_engine as sa_create_engine

    legacy = _make_db(tmp_path / "old" / "iaas.db", "untouched")
    chosen = tmp_path / "chosen.db"
    settings = Settings(
        state_dir=str(tmp_path / "state"), database_url=f"sqlite:///{chosen.as_posix()}"
    )
    monkeypatch.setattr(db_module, "_legacy_database_candidates", lambda: [legacy])
    engine = sa_create_engine(f"sqlite:///{chosen.as_posix()}")

    db_module.relocate_legacy_database(settings, engine)

    assert _read_marker(legacy) == "untouched"
    assert not chosen.exists()
    engine.dispose()


def test_nothing_happens_when_the_engine_points_somewhere_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The second half of the guard, on its own.

    Settings can say "default layout" while the engine actually in use is a
    test's in-memory one — which is exactly the state every test in this suite
    runs in. Moving a file for an engine that will never read it would be pure
    damage.
    """
    from app.config import Settings
    from sqlalchemy import create_engine as sa_create_engine

    legacy = _make_db(tmp_path / "old" / "iaas.db", "untouched")
    settings = Settings(state_dir=str(tmp_path / "state"))
    # The destination has to be *ready*, or a failed move would look exactly
    # like a refused one and this would pass without the guard existing.
    settings.database_path.parent.mkdir(parents=True)
    monkeypatch.setattr(db_module, "_legacy_database_candidates", lambda: [legacy])
    engine = sa_create_engine("sqlite://")  # in-memory, like the suite's own

    db_module.relocate_legacy_database(settings, engine)

    assert _read_marker(legacy) == "untouched"
    assert not settings.database_path.exists()
    engine.dispose()


def test_relocation_is_idempotent(relocation):
    """It runs on every start. The second pass must find nothing to do."""
    run, legacy, target = relocation
    _make_db(legacy, "once")

    run()
    run()

    assert _read_marker(target) == "once"
    assert not legacy.exists()


def test_the_search_looks_where_the_old_default_actually_landed():
    """``uvicorn app.main:app`` only resolves from ``backend/``, so that is
    where a CWD-relative database ended up. Anchored to this package rather
    than to the process CWD, so it is found from wherever you start today."""
    candidates = db_module._legacy_database_candidates()

    backend = Path(db_module.__file__).resolve().parent.parent
    assert backend / "iaas.db" in candidates
    assert Path.cwd().resolve() / "iaas.db" in candidates


def test_the_database_directory_is_created_before_anything_opens_it(
    tmp_path: Path,
):
    """SQLite does not create directories, and the state root does not exist on
    a fresh install. Without this the first start fails with "unable to open
    database file" — naming the file, and not the missing directory."""
    from sqlalchemy import create_engine as sa_create_engine

    nested = tmp_path / "brand" / "new" / "iaas.db"
    engine = sa_create_engine(f"sqlite:///{nested.as_posix()}")

    db_module.ensure_database_directory(engine)

    assert nested.parent.is_dir()
    assert not nested.exists()  # the directory only; the file is SQLite's job
    engine.dispose()
