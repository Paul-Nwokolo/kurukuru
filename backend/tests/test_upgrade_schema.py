"""Upgrading an existing database, which is the path the suite cannot see.

Every other test builds its database with ``create_all``, so every other test
gets the *current* schema. That is the right thing for testing behaviour, and it
is exactly why a migration gap is invisible here: the shape that breaks is the
one nobody's test ever has.

The bug that prompted this file: ``console_tickets`` arrived as a new table, and
the migration table said new tables need no entry because ``create_all`` builds
them complete. True the day the table lands; false the moment a field is added
to it. ``credential_version`` was added later, so every database that already
had the table kept a ``console_tickets`` without it — and since ``create_all``
only ever creates missing *tables*, nothing added the column.

What the user saw was "Request failed with status code 500" on opening a
console, seven hours into a Windows install. Nothing in that mentions a schema.

So these tests do what the rest of the suite cannot: build a database in an
older shape, run the real migration against it, and check the result can be
written to.
"""

from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

import kurukuru.database as database
from kurukuru.database import _ADDED_COLUMNS, _apply_additive_migrations
from kurukuru.models import ConsoleTicket, User


@pytest.fixture()
def old_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A database at the current schema, ready to be aged backwards.

    ``_apply_additive_migrations`` reads the module-level engine rather than
    taking one, so the engine is patched here — the same seam
    ``conftest.isolated_state`` uses.
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    return engine


def _columns(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _drop(engine, table: str, column: str) -> None:
    """Recreate the older shape.

    Any index over the column goes first: SQLite refuses to drop a column an
    index still references, which is a fixture concern rather than anything the
    migration has to handle — it only ever *adds*.
    """
    with engine.begin() as conn:
        for index in inspect(engine).get_indexes(table):
            if column in (index.get("column_names") or []):
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{index["name"]}"')
        conn.exec_driver_sql(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')


@pytest.mark.parametrize(
    ("table", "column"),
    [(t, c) for t, cols in _ADDED_COLUMNS.items() for c, _ddl in cols],
)
def test_every_declared_column_is_actually_added(old_db, table: str, column: str) -> None:
    """Strip each declared column; the migration must restore it.

    Parametrised across the whole table so a new entry is exercised the day it
    is written, rather than only the one that happened to break.
    """
    _drop(old_db, table, column)
    assert column not in _columns(old_db, table), "fixture did not remove the column"

    _apply_additive_migrations()

    assert column in _columns(old_db, table), (
        f"{table}.{column} is declared in _ADDED_COLUMNS but was not added back"
    )


def test_a_console_ticket_can_be_written_after_upgrading(old_db) -> None:
    """The end the user actually reaches: minting a ticket must succeed.

    The column existing is not the claim — the failure was an INSERT, and an
    INSERT is what shows the schema is usable rather than merely present.
    """
    _drop(old_db, "console_tickets", "credential_version")

    with Session(old_db) as session:
        session.add(User(id="u", username="owner", password_hash="x"))
        session.commit()

    def mint(token: str) -> None:
        with Session(old_db) as session:
            session.add(
                ConsoleTicket(
                    token_hash=token,
                    user_id="u",
                    instance_id="i",
                    expires_at=datetime.datetime.now(tz=datetime.timezone.utc),
                )
            )
            session.commit()

    # The production failure, reproduced.
    with pytest.raises((OperationalError, sqlite3.OperationalError)) as exc:
        mint("before")
    assert "credential_version" in str(exc.value)

    _apply_additive_migrations()

    mint("after")
    with Session(old_db) as session:
        assert session.exec(select(ConsoleTicket)).first() is not None


def test_the_migration_covers_every_column_the_models_declare(old_db) -> None:
    """A model field no migration knows about is the whole bug class.

    Ages every table backwards by stripping the columns ``_ADDED_COLUMNS``
    claims to add, migrates, and requires the result to match the models. It
    cannot prove a *future* field will be listed; it does prove the list is
    complete for what exists today, which is the check that was missing when
    ``credential_version`` was added.
    """
    for table, cols in _ADDED_COLUMNS.items():
        for column, _ddl in cols:
            _drop(old_db, table, column)

    _apply_additive_migrations()

    missing = [
        f"{name}.{column.name}"
        for name, table in SQLModel.metadata.tables.items()
        for column in table.columns
        if column.name not in _columns(old_db, name)
    ]
    assert not missing, (
        "these model columns are absent after migrating an older database, so "
        f"an upgrade would fail on them: {missing}"
    )
