"""
Database bootstrap.

SQLite is the source of truth for *desired* state. The reconciler
(Phase 2) compares this against actual Multipass state.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Generator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from kurukuru.config import Settings, get_settings
from kurukuru.product import CLI_NAME, DATABASE_LEAF, LEGACY_CLI_NAME

#: The stem automatic backups are named with. The command name rather than
#: the distribution name: it is what the user types and what the docs spell.
PRODUCT_SLUG = CLI_NAME

logger = logging.getLogger("kurukuru.db")

#: Built on first use, not at import. See ``__getattr__`` below.
_engine: Engine | None = None


def _build_engine(settings: Settings) -> Engine:
    """The one place an engine is configured.

    ``check_same_thread=False`` is required because FastAPI may service a
    request on a different thread than the one that opened the connection;
    session-per-request (below) keeps that safe.

    ``resolved_database_url``, not ``database_url``: the default lives under
    ``~/.kurukuru``, and SQLAlchemy would open a directory literally named
    ``~``.
    """
    return create_engine(
        settings.resolved_database_url,
        echo=settings.debug,
        connect_args={"check_same_thread": False}
        if settings.database_url.startswith("sqlite")
        else {},
    )


def __getattr__(name: str) -> object:
    """Create ``engine`` on first access rather than at import.

    This module used to do ``settings = get_settings()`` and build the engine
    from it at import time, which is the exact shape CONTRIBUTING's rule 5 and
    DECISIONS #59 are about — and it was still here, in one of the files that
    rule was written about.

    What it cost, measured rather than assumed: with the guard instrumented to
    record every attempt, the whole suite reached the real database exactly
    twice, and both were the two tests that exist to prove the guard fires. So
    the *engine* was not the leak — ``conftest.isolated_state`` patches
    ``kurukuru.database.engine`` and that held. But the correctness of an
    import-time value depended on a fixture remembering to overwrite it, which
    is the hand-maintained arrangement DECISIONS #59 says to stop relying on;
    and the pragma listener below read that snapshot at *connect* time, so a
    test engine's pragmas were decided by the developer's real settings.

    Lazy, so nothing is read from settings until something actually wants a
    database. PEP 562: this fires only for names not already in the module, and
    the result is cached into globals, so the second access is an ordinary
    attribute lookup and ``monkeypatch.setattr`` still works exactly as before.
    """
    if name == "engine":
        global _engine
        if _engine is None:
            _engine = _build_engine(get_settings())
        globals()["engine"] = _engine
        return _engine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # noqa: ANN001
    """Enable WAL + foreign keys for better concurrency and integrity.

    ``get_settings()`` at call time, not a module-level snapshot: this fires on
    every connection, including connections to engines a test built, and the
    snapshot was the developer's real configuration. It happened to be
    harmless — both URLs start with ``sqlite`` — which is the only reason it
    was never noticed.
    """
    if get_settings().database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# Columns added after the first release. ``create_all`` only creates *missing
# tables*, so an existing database would keep its old shape and every query
# naming a new column would fail.
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "instances": [
        ("engine", "VARCHAR(16) NOT NULL DEFAULT 'multipass'"),  # Phase 5
        ("ssh_port", "INTEGER"),
        ("vnc_port", "INTEGER"),
        ("qmp_port", "INTEGER"),
        ("pid", "INTEGER"),
        # Phase 6
        ("boot_source", "VARCHAR(5) NOT NULL DEFAULT 'IMAGE'"),
        ("accel", "VARCHAR(8)"),
        ("iso", "VARCHAR"),
        ("image_id", "VARCHAR"),
        ("display", "VARCHAR(8)"),  # Phase 7
        ("cpus", "INTEGER"),
        ("memory_mb", "INTEGER"),
        ("disk_gb", "INTEGER"),
        ("ssh_enabled", "BOOLEAN"),
        ("user_data", "VARCHAR"),  # Phase 10
        ("project_id", "VARCHAR"),  # Phase 11
        ("network_id", "VARCHAR"),
        ("guest_os", "VARCHAR(8) NOT NULL DEFAULT 'LINUX'"),  # Phase 13
        ("qemu_version", "VARCHAR"),
        ("monitor_reachable", "BOOLEAN"),  # Phase 14
    ],
    "images": [("project_id", "VARCHAR")],
    "keypairs": [("project_id", "VARCHAR")],
    # `volumes`, `networks` and `port_forwards` are new tables; create_all
    # builds them with the full schema.
}


# Indexes whose *definition* changed after release. ``create_all`` skips any
# index whose name already exists, so a redefinition has to be applied by hand.
#
# SQLite cannot drop a table-level constraint in place (that needs the
# create-copy-swap dance), but an index is not a table constraint: DROP INDEX /
# CREATE INDEX is fully supported and rewrites nothing but the index itself.
# The uniqueness of `instances.name` lives in an index, so that is all this
# needs — no table rebuild, no row copying, no window where data is in flight.
_REDEFINED_INDEXES: dict[str, list[tuple[str, str]]] = {
    "instances": [
        # Was UNIQUE. Dropped so a name can be reused once its instance is
        # Terminated; the create route now enforces uniqueness across live rows
        # only. The index stays because reconciliation looks rows up by name.
        ("ix_instances_name", "CREATE INDEX ix_instances_name ON instances (name)"),
    ],
}


def _backfill_instance_sizing(connection) -> None:
    """Expand each pre-Phase-7 row's preset label into explicit numbers.

    Sizing moved from a preset *reference* to numbers on the row. Existing rows
    only recorded the label, so the catalog is consulted once, here — after
    which the row stands on its own and a later edit to a preset cannot silently
    change what an existing instance claims to be.
    """
    from kurukuru.config import get_settings

    presets = get_settings().flavors
    pending = connection.execute(
        text("SELECT DISTINCT flavor FROM instances WHERE cpus IS NULL")
    ).fetchall()
    for (label,) in pending:
        spec = presets.get((label or "").lower())
        if spec is None:
            # An unknown label (a preset since renamed or removed). Leaving the
            # numbers null is honest: we do not know what it was, and inventing
            # a size would misreport history.
            logger.warning("Cannot expand unknown preset '%s'; leaving sizing null", label)
            continue
        logger.info("Migrating rows with preset '%s' to explicit sizing", label)
        connection.execute(
            text(
                "UPDATE instances SET cpus = :cpus, memory_mb = :memory_mb, "
                "disk_gb = :disk_gb WHERE cpus IS NULL AND flavor = :flavor"
            ),
            {"cpus": spec.cpus, "memory_mb": spec.memory_mb,
             "disk_gb": spec.disk_gb, "flavor": label},
        )


def _seed_default_project(connection) -> None:
    """Ensure a default project exists and adopt every unfiled resource into it.

    Runs on every start, and is idempotent twice over: it creates the row only
    if no default exists, and it only ever fills in nulls.

    Backfilling matters more than it looks. ``project_id`` is nullable, and the
    API reads a null as "the default project" — so an install that skipped this
    would still work. But the *filters* would not: selecting a project asks for
    rows whose ``project_id`` matches, and an existing install's instances would
    match nothing and vanish from a filtered view. Adopting them explicitly is
    what makes "my VMs disappeared after upgrading" not happen.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    row = connection.execute(
        text("SELECT id FROM projects WHERE is_default = 1 LIMIT 1")
    ).fetchone()

    if row is None:
        project_id = str(_uuid.uuid4())
        logger.info("Seeding the default project")
        connection.execute(
            text(
                "INSERT INTO projects (id, name, description, is_default, created_at) "
                "VALUES (:id, :name, :description, 1, :created_at)"
            ),
            {
                "id": project_id,
                "name": "default",
                "description": "Everything that has not been filed anywhere else.",
                "created_at": datetime.now(timezone.utc),
            },
        )
    else:
        project_id = row[0]

    _seed_default_network(connection)

    for table in ("instances", "images", "keypairs"):
        result = connection.execute(
            text(f"UPDATE {table} SET project_id = :pid WHERE project_id IS NULL"),
            {"pid": project_id},
        )
        if result.rowcount:
            logger.info(
                "Migrated %d %s row(s) into the default project", result.rowcount, table
            )


def _seed_default_network(connection) -> None:
    """Ensure the default user network exists and adopt every instance onto it.

    Same reasoning as the default project: ``network_id`` is nullable and the
    API reads a null as "the default network", so an install that skipped this
    would work — but a filtered or grouped view would show existing instances
    as belonging to nothing. They have always been on QEMU's user-mode NAT;
    this says so.
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    row = connection.execute(
        text("SELECT id FROM networks WHERE is_default = 1 LIMIT 1")
    ).fetchone()

    if row is None:
        network_id = str(_uuid.uuid4())
        logger.info("Seeding the default user network")
        connection.execute(
            text(
                "INSERT INTO networks (id, name, mode, cidr, is_default, created_at) "
                "VALUES (:id, :name, :mode, :cidr, 1, :created_at)"
            ),
            {
                "id": network_id,
                "name": "default",
                # SQLAlchemy Enum columns persist the member *name*, so a hand
                # written INSERT has to match that spelling, not the value.
                "mode": "USER",
                "cidr": "10.0.2.0/24",
                "created_at": datetime.now(timezone.utc),
            },
        )
    else:
        network_id = row[0]

    result = connection.execute(
        text("UPDATE instances SET network_id = :nid WHERE network_id IS NULL"),
        {"nid": network_id},
    )
    if result.rowcount:
        logger.info("Attached %d instance(s) to the default network", result.rowcount)


# --------------------------------------------------------------------------- #
# Pre-migration backups
# --------------------------------------------------------------------------- #
#: Automatic backups are named so that pruning can glob for exactly the files
#: this module wrote. The directory is shared with hand-made backups (Phase 13
#: left one there), and deleting somebody's manual copy because it happened to
#: sit in the same folder would be a poor trade for tidiness.
#:
#: Two prefixes, because Phase 16 renamed the product and the backups this
#: module wrote under the old one are still *ours*. Leaving them unmatched would
#: reclassify them as hand-made and exempt them from retention forever, so the
#: pruning glob covers both — and sorts on the timestamp rather than the whole
#: filename, which would otherwise order every "iaas-" backup before every
#: "kurukuru-" one regardless of when they were taken.
_BACKUP_PREFIXES = (f"{PRODUCT_SLUG}-", f"{LEGACY_CLI_NAME}-")
_BACKUP_PREFIX = _BACKUP_PREFIXES[0]
_BACKUP_SUFFIX = "-pre-migration.db"
_BACKUP_GLOBS = tuple(f"{prefix}*{_BACKUP_SUFFIX}" for prefix in _BACKUP_PREFIXES)


def _backup_stamp(path: Path) -> str:
    """The timestamp inside a backup filename, for ordering across prefixes."""
    name = path.name
    for prefix in _BACKUP_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


@dataclass(frozen=True)
class PendingMigration:
    """What ``_apply_additive_migrations`` is about to change, if anything.

    Computed *before* the work so the answer can gate a backup. Truthiness is
    the question every caller actually asks: is this startup a no-op?
    """

    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    indexes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.columns or self.indexes)

    def describe(self) -> str:
        parts = [f"{table}: add {', '.join(cols)}"
                 for table, cols in sorted(self.columns.items())]
        parts += [f"{table}: redefine {', '.join(idx)}"
                  for table, idx in sorted(self.indexes.items())]
        return "; ".join(parts) if parts else "nothing"


def _pending_migrations(inspector, existing_tables: set[str]) -> PendingMigration:
    """Which columns and indexes the migration below would actually touch.

    Deliberately mirrors the apply loop's conditions rather than approximating
    them: if the two ever disagree, either a backup is taken for a startup that
    changes nothing, or -- much worse -- a schema change lands without one.
    """
    columns: dict[str, tuple[str, ...]] = {}
    indexes: dict[str, tuple[str, ...]] = {}

    for table, wanted in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all just built it with the full schema
        present = {col["name"] for col in inspector.get_columns(table)}
        missing = tuple(name for name, _ddl in wanted if name not in present)
        if missing:
            columns[table] = missing

    for table, wanted in _REDEFINED_INDEXES.items():
        if table not in existing_tables:
            continue
        current = {idx["name"]: idx for idx in inspector.get_indexes(table)}
        stale = tuple(
            name for name, _ddl in wanted
            if current.get(name) is None or current[name].get("unique")
        )
        if stale:
            indexes[table] = stale

    return PendingMigration(columns=columns, indexes=indexes)


def _unique_backup_path(directory: Path, stamp: str) -> Path:
    """A backup filename that does not already exist.

    Two migrations inside the same second is contrived, but a collision would
    silently overwrite the older backup -- the one thing a backup must never do.
    """
    candidate = directory / f"{_BACKUP_PREFIX}{stamp}{_BACKUP_SUFFIX}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{_BACKUP_PREFIX}{stamp}-{counter}{_BACKUP_SUFFIX}"
        counter += 1
    return candidate


def _prune_backups(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` automatic backups. Never anything else.

    Only files matching this module's own naming patterns are considered, and
    only files -- the directory also holds hand-made backups, which are somebody
    else's decision to keep. Ordering is by the timestamp *inside* the name, not
    by the name itself, so a backup written before the Phase 16 rename sorts
    among the ones written after it rather than ahead of all of them.
    """
    if keep <= 0:
        return []
    ours = sorted(
        {
            path
            for pattern in _BACKUP_GLOBS
            for path in directory.glob(pattern)
            if path.is_file()
        },
        key=_backup_stamp,
    )
    removed: list[Path] = []
    for path in ours[:-keep] if len(ours) > keep else []:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning("Could not prune old backup %s: %s", path, exc)
        else:
            removed.append(path)
    return removed


def backup_database_file(
    source: Path, directory: Path, *, retention: int = 5
) -> Path | None:
    """Snapshot one SQLite file into ``directory``. Returns the path, or None.

    The mechanism, without the settings. Split out from :func:`backup_database`
    so :mod:`kurukuru.state_migration` can use it: the state-dir migration has to
    back up a database that is **not** the one settings describe -- it is still
    at the old path, under the old name, which is the entire reason a backup is
    being taken -- and the guards on the settings-aware wrapper exist precisely
    to stop it copying a database that is not the configured one.

    **Why not copy the file.** A live backend may be mid-write, and with WAL
    journalling the committed state is spread across the ``.db`` and its
    ``-wal``. Copying them one at a time gives a pair from two different moments
    -- exactly the failure a backup exists to prevent. The online backup API
    instead reads a consistent snapshot through SQLite itself, over a separate
    connection so an in-flight writer is not blocked, and folds the WAL contents
    in. The result is one self-contained ``.db`` file with **no sidecars to keep
    with it**, which is also what makes the documented restore a single copy.

    Returns None, loudly, when it could not run. A backup failure does not stop
    the caller: the migrations it guards are additive or are directory moves
    that lose nothing, and refusing to proceed because a backup directory is
    unwritable would turn a precaution into an outage. The error names the path.
    """
    if not _has_rows(source):
        return None  # nothing worth copying yet

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination = _unique_backup_path(directory, stamp)
        origin = sqlite3.connect(str(source))
        try:
            copy = sqlite3.connect(str(destination))
            try:
                origin.backup(copy)
            finally:
                copy.close()
        finally:
            origin.close()
    except (OSError, sqlite3.Error) as exc:
        logger.error(
            "Could not back up %s to %s: %s. Continuing -- there is no restore "
            "point for this change.", source, directory, exc,
        )
        return None

    logger.info("Backed up the database to %s (%d bytes)",
                destination, destination.stat().st_size)
    for pruned in _prune_backups(directory, retention):
        logger.info("Pruned old backup %s", pruned.name)
    return destination


def backup_database(settings_in_use: Settings, engine_in_use: Engine) -> Path | None:
    """Snapshot the configured database before a schema change. Returns the path.

    A thin, heavily guarded wrapper over :func:`backup_database_file`. The
    guards are the point:

    * the source is derived from the **engine**, not from settings, because the
      engine is what will actually be opened -- and in the test suite it is
      routinely not the one settings describe;
    * and it must *also* be the file settings names, or nothing happens. A
      backup routine that trusted settings alone would write copies of a
      ``tmp_path`` database into the developer's real state directory on every
      test run.
    """
    source = _engine_file(engine_in_use)
    if source is None or source != settings_in_use.database_path:
        return None  # in-memory, a server URL, or an engine of the suite's own

    return backup_database_file(
        source,
        Path(settings_in_use.db_backup_dir).expanduser(),
        retention=settings_in_use.db_backup_retention,
    )


def _apply_additive_migrations() -> None:
    """Bring an existing database up to the current schema. Idempotent.

    A full migration tool is overkill for a single-file SQLite database; adding
    columns and redefining indexes covers every change so far.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # Before anything is altered, and only when something will be: an ordinary
    # startup against a converged database must not leave an identical copy
    # behind, or the retention window fills with noise and the one useful
    # restore point ages out of it.
    pending = _pending_migrations(inspector, existing_tables)
    if pending:
        logger.info("Schema changes pending -- %s", pending.describe())
        backup_database(get_settings(), engine)

    with engine.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue  # create_all just built it with the full schema
            present = {col["name"] for col in inspector.get_columns(table)}
            for name, ddl in columns:
                if name in present:
                    continue
                logger.info("Migrating %s: adding column %s", table, name)
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

        for table, indexes in _REDEFINED_INDEXES.items():
            if table not in existing_tables:
                continue
            current = {idx["name"]: idx for idx in inspector.get_indexes(table)}
            for name, ddl in indexes:
                existing = current.get(name)
                if existing is not None and not existing.get("unique"):
                    continue  # already the shape we want
                if existing is not None:
                    logger.info("Migrating %s: dropping unique index %s", table, name)
                    connection.execute(text(f"DROP INDEX {name}"))
                connection.execute(text(ddl))

        _backfill_instance_sizing(connection)
        # After the columns exist, and inside the same transaction, so a
        # half-migrated database is never visible.
        _seed_default_project(connection)


# --------------------------------------------------------------------------- #
# Relocating a pre-state_dir database
# --------------------------------------------------------------------------- #
#: SQLite's write-ahead sidecars. They belong to the ``.db`` file and must
#: travel with it: a WAL left behind is unreplayed committed transactions, and
#: a WAL beside the *wrong* database is a corrupt pair.
_SQLITE_SIDECARS = ("-wal", "-shm")


def _legacy_database_candidates() -> list[Path]:
    """Where a database written by the CWD-relative default could be.

    The old default was ``sqlite:///./iaas.db`` — resolved against whatever
    directory the backend was started from, which is unknowable after the fact.
    Two places are worth looking:

    * ``backend/iaas.db``, anchored to this package rather than to the process
      CWD. This is where it actually lands, because ``uvicorn kurukuru.main:app``
      has to be run from ``backend/`` for the import to resolve.
    * ``./iaas.db``, for a start from somewhere else entirely.

    Ordered: the package-anchored one first, because it is the one that will
    have the real history in it.
    """
    package_root = Path(__file__).resolve().parent.parent  # backend/
    candidates = [package_root / "iaas.db", Path.cwd() / "iaas.db"]
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _has_rows(path: Path) -> bool:
    """Whether a file is a database with something in it.

    A zero-byte file is a valid empty SQLite database, and one gets created by
    the mere act of pointing at a path — which is exactly how the stray this
    replaces came to exist. Treating it as "not really there" is what lets a
    real database move into its place instead of being blocked by it.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _engine_file(engine_in_use: Engine) -> Path | None:
    """The file an engine reads, or None if it is not backed by one.

    Derived from the engine rather than from settings on purpose: the engine is
    what will actually be opened, and in the test suite it is routinely not the
    one settings describe.
    """
    url = engine_in_use.url
    if url.get_backend_name() != "sqlite" or not url.database:
        return None  # a server URL, or in-memory
    return Path(url.database)


def ensure_database_directory(engine_in_use: Engine) -> None:
    """Create the directory the database file lives in.

    The default path moved under ``state_dir``, which on a fresh install does
    not exist yet — and SQLite does not create directories. Without this the
    first start fails with "unable to open database file", naming the file and
    not the missing directory.
    """
    path = _engine_file(engine_in_use)
    if path is None or path.parent == Path(""):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create the database directory %s: %s", path.parent, exc)


def relocate_legacy_database(settings_in_use: Settings, engine_in_use: Engine) -> None:
    """Move a pre-``state_dir`` database to where the backend now looks.

    Runs before anything opens a connection. Deliberately narrow:

    * **Only on the default layout.** If ``IAAS_DATABASE_URL`` names a path,
      that is a decision and nothing here second-guesses it — the same rule
      ``_apply_state_dir`` follows.
    * **Only when the engine about to be used is the one pointing there.**
      Belt to that braces, and it is what keeps a file-moving migration away
      from the test suite: every test runs against an engine of its own, so
      neither half of this condition holds and the scan never starts. A guard
      with one condition would be one careless fixture away from moving a
      developer's real database into a ``tmp_path``.
    * **Never over data.** A target that already has rows wins; the legacy file
      is left untouched and reported, because two databases with history in
      them is a question for a human, not something to resolve by picking one.
    * **Sidecars travel with the file**, and the ``.db`` moves *last*, so an
      interruption leaves the original pair intact rather than a database
      separated from its WAL.

    Anything that goes wrong is logged and swallowed. A backend that cannot
    move an old file should still start against the new one — loudly, so the
    old path is on the record either way.
    """
    target = settings_in_use.database_path
    if target is None or target != settings_in_use.default_database_path:
        return  # not a SQLite file, or an explicitly configured location
    if _engine_file(engine_in_use) != target:
        return  # this engine is not the one that would read the moved file

    for legacy in _legacy_database_candidates():
        if legacy == target or not _has_rows(legacy):
            continue

        if _has_rows(target):
            logger.warning(
                "Found an older database at %s, but %s already has data — leaving "
                "both alone. The backend is using %s; delete or merge the other "
                "once you have decided which history you want.",
                legacy, target, target,
            )
            return

        logger.info("Moving the database from %s to %s", legacy, target)
        try:
            for suffix in _SQLITE_SIDECARS:
                sidecar = Path(f"{legacy}{suffix}")
                if sidecar.exists():
                    shutil.move(str(sidecar), f"{target}{suffix}")
            shutil.move(str(legacy), str(target))
        except OSError as exc:
            logger.error(
                "Could not move %s to %s: %s. The old database is still there; "
                "move it by hand (with its -wal and -shm files) or set "
                "IAAS_DATABASE_URL=sqlite:///%s to keep using it where it is.",
                legacy, target, exc, legacy.as_posix(),
            )
        else:
            logger.info("Database moved. Its history came with it.")
        return


def init_db() -> None:
    """Create all tables and apply additive migrations. Safe on every startup."""
    # Import models so SQLModel.metadata is populated before create_all.
    from kurukuru import models  # noqa: F401
    from kurukuru.state_migration import migrate_state_dir

    # All three before the first connection, and in this order.
    #
    # The state-dir migration goes first because it is the one that moves the
    # *whole tree*, database included, and it refuses to write onto a target
    # that already has contents. ``ensure_database_directory`` would create that
    # target — an empty ``~/.kurukuru`` holding nothing but a directory — and the
    # migration would then decline to move a real install into it. Then the
    # backend would come up on a fresh empty database beside twenty gigabytes of
    # the user's VMs, reporting an install that had lost everything.
    #
    # It raises rather than returning on the one case it cannot handle (running
    # VMs), and that exception is allowed to reach the lifespan. See
    # kurukuru.state_migration.StateMigrationBlocked for why not starting is right.
    migrate_state_dir(get_settings(), engine)

    # Then the directory, then the older CWD-relative relocation — which still
    # looks for a file named `iaas.db`, deliberately: that is the name the old
    # default actually wrote, and renaming the thing being searched for would
    # simply stop finding it.
    ensure_database_directory(engine)
    relocate_legacy_database(get_settings(), engine)

    SQLModel.metadata.create_all(engine)
    _apply_additive_migrations()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a per-request session."""
    with Session(engine) as session:
        yield session
