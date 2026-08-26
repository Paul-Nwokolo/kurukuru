"""
Shared fixtures.

Two things live here, and both exist because a test that can reach the real
machine eventually will.

**Isolation is the default, not an opt-in.** Every test runs against a settings
object and a database rooted in its own ``tmp_path``, applied by an autouse
fixture that *discovers* which modules to redirect rather than naming them. The
previous design was opt-in per module, and it failed twice in the way opt-in
designs fail: Phase 10 wrote generated SSH keypairs into the developer's
``~/.kurukuru/keys`` (ten orphans before anyone looked), and Phase 11 wrote
130 event rows into the real database, which surfaced as test instances named
``web-one`` and ``doomed`` appearing in the live dashboard's activity feed.
Neither was a bug in the code under test. Both were a new module that nobody
remembered to add to a list.

**And a guard, because isolation that silently stops working is worse than
none.** It is the backstop for what this file cannot prevent — a module
imported after the fixture ran, a subprocess, an absolute path written by hand
— and it works differently for the two things being protected:

* The **database is prevented**. Opening the real database raises on the
  spot, with a traceback pointing at the line that did it.
* The **state directories are detected**, fingerprinted before and after every
  test. There is no single call to intercept for "wrote a file somewhere", so
  the stray file does get written; the run fails and names the test to blame.

The database gets the stronger mechanism because it needed one: mtime cannot
distinguish "this test wrote" from "the developer's own backend wrote", and
that backend's reconciler writes every 30 seconds.

A test that genuinely needs the real paths marks itself ``@pytest.mark.
real_state``. Nothing does today, and anything that ever does should have to
explain why in the same commit.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlmodel.pool import StaticPool

from kurukuru.config import DEFAULT_STATE_DIR, Settings, get_settings
from kurukuru.product import LEGACY_STATE_DIR
from kurukuru.host_capacity import invalidate_cache

# --------------------------------------------------------------------------- #
# What "the real machine" means, resolved once
# --------------------------------------------------------------------------- #
#: The state tree a developer's own install uses.
REAL_STATE_DIR = Path(DEFAULT_STATE_DIR).expanduser()

#: And the one Phase 16 renamed it from, which is still sitting there on any
#: machine that has not been upgraded yet — including, until it runs, this one.
#: Watched because the state-dir migration *moves whole directories*: a test
#: that reached it would not leave a stray file behind, it would relocate the
#: developer's entire install. The guards in ``migrate_state_dir`` are what
#: prevent that; this is how we would find out if they stopped working.
LEGACY_REAL_STATE_DIR = Path(LEGACY_STATE_DIR).expanduser()

#: The live database. ``_REAL_DB`` is the path connections are refused to;
#: the WAL sidecars are named alongside it because a write lands there first,
#: which is what makes watching the ``.db`` file's mtime insufficient — see
#: :func:`_real_state_fingerprint` for why that approach was abandoned anyway.
#:
#: Read through ``database_path`` rather than by stripping the URL prefix. The
#: default now lives under ``~``, and ``Path("~/...").resolve()`` produces a
#: directory named ``~`` inside the CWD — a path nothing will ever open, which
#: would have left the guard below matching nothing and reporting green.
_REAL_DB = (Settings().database_path or Path("kurukuru.db")).resolve()
REAL_DB_FILES = (_REAL_DB, Path(f"{_REAL_DB}-wal"), Path(f"{_REAL_DB}-shm"))

#: Names inside the state root that belong to the database and must be excluded
#: from the directory listing below. The database moved under the state root,
#: and its WAL sidecars appear and disappear as connections open and close — a
#: developer's own backend starting or stopping mid-run would otherwise drift
#: the fingerprint and blame whichever test straddled it. Same reasoning that
#: keeps the database off the mtime watch; see :func:`_real_state_fingerprint`.
_DB_SIDECAR_NAMES = frozenset(path.name for path in REAL_DB_FILES)


def _stat(path: Path) -> tuple | None:
    """(size, mtime) for a file, or None if it does not exist."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


def _fingerprint(
    files: Sequence[Path],
    directories: Sequence[Path],
    ignore: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Size+mtime of each file, and the entry list of each directory.

    Parameterised so it can be tested against a temporary tree. Verifying the
    detector by writing to the paths it watches would mean writing into a live
    database's WAL, which is a good way to corrupt the thing being protected.

    ``ignore`` drops entry names from the directory listings — for the database
    and its sidecars, which live in the state root but are guarded by
    :func:`_forbid_real_database_connections` instead.
    """
    fingerprint: dict[str, object] = {str(path): _stat(path) for path in files}
    for directory in directories:
        try:
            entries = sorted(e for e in os.listdir(directory) if e not in ignore)
            fingerprint[str(directory)] = tuple(entries)
        except OSError:
            fingerprint[str(directory)] = None
    return fingerprint


def _real_state_fingerprint() -> dict[str, object]:
    """Everything on disk a test could disturb.

    Directories only, and deliberately shallow: the state root catches a new
    directory appearing, and the two that have actually been written to by
    accident are listed by name. Walking the whole tree would stat a
    multi-gigabyte instances directory on every test.

    The database is **not** watched by mtime, though it was at first. A
    developer's own backend is usually running while they run the tests, and
    its reconciler writes every 30 seconds — so the fingerprint drifted on its
    own and blamed whichever test happened to straddle a reconcile pass. That
    is worse than no guard: a check that cries wolf gets deleted. The database
    is protected by :func:`_forbid_real_database_connections` instead, which
    catches the offender in the act rather than inferring it from a timestamp.
    """
    return _fingerprint(
        (),
        (
            REAL_STATE_DIR,
            REAL_STATE_DIR / "keys",
            REAL_STATE_DIR / "cloud-init",
            LEGACY_REAL_STATE_DIR,
        ),
        ignore=_DB_SIDECAR_NAMES,
    )


def _forbid_real_database_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make opening the real database an immediate, explained failure.

    Prevention rather than detection, and precise where a fingerprint cannot
    be: this fires in the offending test, on the offending line, with a
    traceback pointing at whatever reached the real path — and it cannot be
    confused with another process using the same file.

    Patched on **both** ``sqlite3`` and ``sqlite3.dbapi2``. They are separate
    module objects and SQLAlchemy's pysqlite dialect imports the latter
    (``from sqlite3 import dbapi2 as sqlite``), so patching only the obvious
    one leaves every engine unguarded — which is exactly what happened on the
    first attempt, and the test below is why it was noticed.
    """
    import sqlite3
    import sqlite3.dbapi2

    def _guard(real_connect):
        def guarded(database, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            try:
                target = Path(str(database)).resolve()
            except (OSError, ValueError):
                target = None
            if target == _REAL_DB:
                raise RuntimeError(
                    f"A test tried to open the real database at {_REAL_DB}.\n"
                    "Tests are isolated to tmp_path by conftest.isolated_state; "
                    "something built its own engine or session against the real "
                    "URL. Use the engine the fixture provides, or mark the test "
                    "@pytest.mark.real_state and say why."
                )
            return real_connect(database, *args, **kwargs)

        return guarded

    for module in (sqlite3, sqlite3.dbapi2):
        monkeypatch.setattr(module, "connect", _guard(module.connect))


def _describe_drift(before: dict, after: dict) -> str:
    lines = []
    for key, old in before.items():
        new = after.get(key)
        if old != new:
            lines.append(f"  {key}\n    before: {old}\n    after:  {new}")
    return "\n".join(lines)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_state: test may read or write the real ~/.kurukuru state and "
        "database. Opts out of the isolation every other test gets by default.",
    )


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #
def _app_modules_with(attribute: str):
    """Every imported ``kurukuru.*`` module holding its own reference to something.

    ``from kurukuru.database import engine as db_engine`` binds a *copy* of the
    name, so patching ``kurukuru.database.engine`` alone leaves every importer still
    pointing at the real database. The same is true of ``get_settings``.

    Discovered rather than listed, which is the entire point of this file: a
    module added next phase is covered the moment it is imported, and cannot be
    forgotten because there is nothing to remember.
    """
    import sys

    return [
        module
        for name, module in list(sys.modules.items())
        if name == "kurukuru" or name.startswith("kurukuru.")
        if module is not None and hasattr(module, attribute)
    ]


def redirect_db_engines(monkeypatch: pytest.MonkeyPatch, engine) -> None:
    """Point every module holding a ``db_engine`` at ``engine``.

    Exported for the per-module client fixtures, which build their own
    in-memory database and need the background jobs writing into the *same*
    one their request sessions read from. They used to name the modules in a
    tuple, and that tuple is what was forgotten in Phase 11 — and again in
    Phase 12, when a new projects router made the default project land in a
    different database than the tests read from.

    Same discovery as :func:`isolated_state` uses, so there is one mechanism
    and nothing to keep in step.
    """
    for module in _app_modules_with("db_engine"):
        monkeypatch.setattr(module, "db_engine", engine, raising=False)


@pytest.fixture(autouse=True)
def isolated_state(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch):
    """Point settings and the database at this test's own tmp_path.

    Runs for every test. Fixtures that build their own engine (most of the
    router suites do) layer on top of this and win for their own purposes; this
    is the floor, so a path nobody thought about lands in tmp rather than in
    the developer's home directory.
    """
    if request.node.get_closest_marker("real_state"):
        yield
        return

    state = tmp_path / "state"
    database = tmp_path / "kurukuru.db"

    # The CLI reads its API token from a file resolved through the environment,
    # so without this a test run on a machine where somebody has signed in picks
    # up that person's real credential — and then fails, confusingly, because
    # the test database has never heard of it. Same rule as the rest of this
    # fixture: a test must not be able to reach the state of the machine
    # running it, in either direction.
    monkeypatch.setenv("KURUKURU_AUTH_TOKEN_FILE", str(tmp_path / "cli-token"))
    settings = Settings(
        state_dir=str(state),
        database_url=f"sqlite:///{database.as_posix()}",
    )

    # In-memory on a StaticPool: every session in every thread shares one
    # connection, which is what makes ":memory:" behave like a single database
    # rather than a fresh empty one per connection. A file would work too and
    # was the first version — it cost 20s across the suite in fsyncs, for a
    # fallback engine most tests never touch because their own fixture
    # overrides it.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    get_settings.cache_clear()
    monkeypatch.setattr("kurukuru.config.get_settings", lambda: settings)
    monkeypatch.setattr("kurukuru.database.engine", engine)
    for module in _app_modules_with("get_settings"):
        monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    for module in _app_modules_with("db_engine"):
        monkeypatch.setattr(module, "db_engine", engine, raising=False)

    # The FastAPI dependency, for any test that drives TestClient without
    # overriding it — otherwise requests would still resolve the real session.
    from kurukuru.database import get_session
    from kurukuru.main import app as api_app

    def _session_override():
        from sqlmodel import Session

        with Session(engine) as session:
            yield session

    had_override = get_session in api_app.dependency_overrides
    if not had_override:
        api_app.dependency_overrides[get_session] = _session_override

    try:
        yield settings
    finally:
        if not had_override:
            api_app.dependency_overrides.pop(get_session, None)
        engine.dispose()
        get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def no_real_state_writes(request: pytest.FixtureRequest, monkeypatch):
    """Fail the test that touches the real install, and name it.

    Two mechanisms, because the two things being protected fail differently.
    The database is *prevented* — opening it raises on the spot. The state
    directories are *detected*, sampled before and after each test, because
    there is no single call to intercept for "wrote a file somewhere".

    Sampled per test rather than per session on purpose. A session-level check
    would say only that *something* wrote, leaving the search to a human; this
    points at the test. The cost is two listdirs per test.

    Re-baselined every test, so one offending test produces one failure rather
    than turning every subsequent test red.
    """
    if request.node.get_closest_marker("real_state"):
        yield
        return

    _forbid_real_database_connections(monkeypatch)

    before = _real_state_fingerprint()
    yield
    after = _real_state_fingerprint()

    if before != after:
        pytest.fail(
            "This test wrote to the real install. Tests are isolated to "
            "tmp_path by conftest.isolated_state; something reached past it — "
            "an absolute path, a subprocess, or a module imported after the "
            "fixture ran.\n\n"
            f"{_describe_drift(before, after)}\n\n"
            "If the access is genuinely intended, mark the test "
            "@pytest.mark.real_state and say why.",
            pytrace=False,
        )


@pytest.fixture()
def small_host():
    """Pin the host to 8 cores / 16 GB / 500 GB free.

    Capacity assertions must not depend on whatever the machine running the
    tests happens to have free at that moment.

    Every client fixture now depends on this, not just the tests that assert on
    capacity directly — because *any* test that launches an instance is a
    capacity assertion whether it means to be or not. The first Linux run
    proved it: on a host with 4.8 GB free, the ``small`` preset's 5 GB disk was
    correctly refused and 60-odd tests failed with `KeyError: 'id'` several
    frames away from the cause. Nothing was wrong with the code, and nothing in
    the failure said so.

    Pinned high enough that no preset can exceed it, so a test that wants a
    refusal has to ask for one explicitly.
    """
    vm = type("VM", (), {"total": 16384 * 1024**2, "available": 8192 * 1024**2})()
    invalidate_cache()
    with (
        patch("kurukuru.host_capacity.psutil.cpu_count", return_value=8),
        patch("kurukuru.host_capacity.psutil.virtual_memory", return_value=vm),
        patch("kurukuru.host_capacity._disk_free_bytes",
              return_value=(1000 * 1024**3, 500 * 1024**3)),
    ):
        yield
    invalidate_cache()


def authenticate_test_client(client, engine) -> str:
    """Give a TestClient the owner account and a Bearer token for it.

    Phase 15 closed the API by default, so a client that does not authenticate
    can only assert 401s. Every per-module client fixture calls this, once,
    rather than several hundred tests each learning about credentials.

    A Bearer token rather than a session cookie on purpose: token requests are
    exempt from the CSRF check (a browser cannot be tricked into attaching an
    Authorization header cross-origin), so existing state-changing tests do not
    each need a CSRF header bolted on. The cookie path has its own tests.

    Rows are written straight to the database rather than through the login
    route: the first-run and login flows have their own tests, and the rest of
    the suite should not fail when those flows change shape.
    """
    from sqlmodel import Session as _Session

    from kurukuru import auth as _auth
    from kurukuru.models import User as _User

    with _Session(engine) as session:
        user = _User(
            username="owner",
            password_hash=_auth.hash_password("test-password-1234"),
            is_owner=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        _row, secret = _auth.create_api_token(session, user, "test-suite")

    client.headers["Authorization"] = f"Bearer {secret}"
    client.api_token = secret          # type: ignore[attr-defined]
    client.owner_username = "owner"    # type: ignore[attr-defined]
    client.owner_password = "test-password-1234"  # type: ignore[attr-defined]
    return secret
