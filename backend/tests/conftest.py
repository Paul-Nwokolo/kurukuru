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

There is no way to opt out. Until Phase 18 a ``real_state`` marker switched
every guard off for a test, and it is exactly what let the harness's own
self-tests write into the developer's real ``~/.kurukuru/keys`` unnoticed. It
was removed rather than documented: with ``--strict-markers`` a test that asks
for it now fails at collection. A test that needs a "real" path uses a stubbed
one (see ``test_isolation.py``). CONTRIBUTING, "Tests never see your home".
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

#: The real home, resolved once at import — before any test can redirect it.
REAL_HOME = Path.home()

#: Places in a real home the product writes to on Linux (DECISIONS #69, #71)
#: and so places a test can escape to. Phase 18 found the gap the hard way: a
#: test whose "is this path absolute?" check misread a Windows path fell back to
#: Path.home() and wrote a systemd unit into the developer's ~/.config — and
#: this guard, which watched only the state tree, said nothing.
#:
#: Directory *listings* only, and only these. Not the whole home directory: on
#: Windows ``~`` has registry transaction logs appearing and disappearing
#: during a run, and a guard that blames an innocent test gets deleted. The two
#: parents are here because creating them is exactly what that escape did.
REAL_HOME_WATCHED = (
    REAL_HOME / ".config" / "kurukuru",
    REAL_HOME / ".config" / "systemd",
    REAL_HOME / ".config" / "systemd" / "user",
    REAL_HOME / ".local" / "share" / "kurukuru",
)

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
    fingerprint = _fingerprint((), _real_state_watch_list(), ignore=_DB_SIDECAR_NAMES)
    fingerprint.update(_quiescent_mtimes(_real_quiescent_dirs()))
    return fingerprint


def _quiescent_mtimes(directories: Sequence[Path]) -> dict[str, object]:
    """Each directory's own mtime, which moves on any create, delete or rename
    inside it — including a file created and removed again within one test.

    Added in Phase 18, after restoring an old self-test showed the listing
    alone cannot see that: the test built its leak path from REAL_STATE_DIR, an
    absolute real path computed at import, so no HOME redirection could stop
    it, and it removed its stray file before the listing was taken again. It
    moved ~/.kurukuru/keys' mtime, and nothing else noticed.
    """
    stamps: dict[str, object] = {}
    for directory in directories:
        try:
            stamps[f"mtime:{directory}"] = directory.stat().st_mtime_ns
        except OSError:
            stamps[f"mtime:{directory}"] = None
    return stamps


def _real_quiescent_dirs() -> tuple[Path, ...]:
    """Real directories nothing should touch while the suite runs.

    Not the state root: a developer's own backend rewrites its database there
    every 30 seconds, and a guard that blames an innocent test gets deleted.
    These only change when a keypair is generated, a VM is launched, or the
    product is installed — none of which a test run should cause.
    """
    return (
        REAL_STATE_DIR / "keys",
        REAL_STATE_DIR / "cloud-init",
        LEGACY_REAL_STATE_DIR,
        *REAL_HOME_WATCHED,
    )


def _real_state_watch_list() -> tuple[Path, ...]:
    """Which real directories the guard watches — a pure answer, no I/O.

    Separate so the wiring can be tested without listing the developer's real
    state tree at all (test_the_guard_watches_the_key_directory_that_actually_leaked).
    """
    return (
        REAL_STATE_DIR,
        REAL_STATE_DIR / "keys",
        REAL_STATE_DIR / "cloud-init",
        LEGACY_REAL_STATE_DIR,
        *REAL_HOME_WATCHED,
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
                    "URL. Use the engine the fixture provides."
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


# --------------------------------------------------------------------------- #
# The home directory, sandboxed for the whole run
# --------------------------------------------------------------------------- #
#: Every variable through which code finds "the user's home", on either OS.
#: POSIX code reads HOME and the XDG set; Windows code reads USERPROFILE (which
#: is what Path.home() and expanduser use there) and the two AppData roots.
HOME_VARIABLES = (
    "HOME", "USERPROFILE",
    "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
    "APPDATA", "LOCALAPPDATA",
)


@pytest.fixture(scope="session", autouse=True)
def sandboxed_home(tmp_path_factory: pytest.TempPathFactory):
    """Point the home directory at a temporary one, for every test in the run.

    Twice in Phase 18 a test wrote into the developer's real home — the
    isolation self-tests into ``~/.kurukuru/keys``, then a service test into
    ``~/.config/systemd/user`` — and both were found by accident and fixed one at
    a time. That is a pattern, so the fix is structural: no test can resolve the
    real home at all. Set once for the session (subprocesses inherit it), and
    checked before every test by :func:`home_is_the_sandbox`. CONTRIBUTING,
    "Tests never see your home directory".

    ``REAL_HOME`` and the ``REAL_*`` paths above were resolved at import, before
    this runs, so the guards can still say which real paths to protect.
    """
    home = tmp_path_factory.mktemp("home")
    values = {
        "HOME": home,
        "USERPROFILE": home,
        "XDG_DATA_HOME": home / ".local" / "share",
        "XDG_CONFIG_HOME": home / ".config",
        "XDG_STATE_HOME": home / ".local" / "state",
        "XDG_CACHE_HOME": home / ".cache",
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
    }
    with pytest.MonkeyPatch.context() as mp:
        for name in HOME_VARIABLES:
            mp.setenv(name, str(values[name]))
        yield home


#: Caches that *Windows itself* keeps under the home directory, written by the
#: OS tools the code under test legitimately runs — PowerShell's startup-profile
#: cache (fs_permissions runs PowerShell to set ACLs) and the shell's
#: ``Caches``. Windows' own per-user housekeeping, never a write by Kurukuru.
#: One entry, and it must stay that short: anything else in the home directory
#: — above all .kurukuru, .config or .local — still fails the test.
TOOL_CACHE_DIRS = ("AppData/Local/Microsoft/Windows",)


def _home_listing(home: Path) -> list[str]:
    def owned_by_a_tool(rel: str) -> bool:
        for cache in TOOL_CACHE_DIRS:
            # Inside the tool's cache, or an empty parent created on the way to it.
            if rel == cache or rel.startswith(cache + "/") or (cache + "/").startswith(rel + "/"):
                return True
        return False

    listing = []
    for p in home.rglob("*"):
        rel = p.relative_to(home).as_posix()
        if owned_by_a_tool(rel):
            continue
        # An empty AppData/Roaming is created alongside the PowerShell cache.
        if rel in ("AppData", "AppData/Roaming") and p.is_dir() and not any(
            q for q in p.rglob("*") if not owned_by_a_tool(q.relative_to(home).as_posix())
        ):
            continue
        listing.append(rel)
    return sorted(listing)


@pytest.fixture(autouse=True)
def home_is_the_sandbox(request: pytest.FixtureRequest, sandboxed_home: Path):
    """Fail outright if a test can see the real home, or writes to the fake one.

    Two checks, because prevention alone would make an escaping test harmless
    but silent: the bug would still be there, writing into a temporary
    directory nobody looks at. So the sandbox must also be *untouched* — a test
    that needs a home-shaped tree makes its own under ``tmp_path``.

    There is no opt-out from either check.
    """
    seen = {"Path.home()": Path.home(), "expanduser('~')": Path(os.path.expanduser("~"))}
    for how, where in seen.items():
        if where.resolve() == REAL_HOME.resolve():
            pytest.fail(
                f"{how} is the real home directory ({REAL_HOME}). The session "
                f"fixture sandboxed_home should have redirected it; something "
                f"has undone that, and every test after this point could write "
                f"into the developer's own home. Refusing to run.",
                pytrace=False,
            )
    before = _home_listing(sandboxed_home)
    yield
    after = _home_listing(sandboxed_home)
    if after != before:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        # Clean up so the next test starts from an empty home — this is a
        # temporary directory created for the run, never a real one.
        for name in reversed(added):
            target = sandboxed_home / name
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                try:
                    target.rmdir()
                except OSError:
                    pass
        pytest.fail(
            "This test wrote into the home directory. In a normal run that is "
            "the developer's real home; here it was a sandbox, so nothing was "
            "harmed — but the code path that did it is real. Use tmp_path, or "
            "pass the directory in.\n"
            f"  added:   {added}\n  removed: {removed}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def isolated_state(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch):
    """Point settings and the database at this test's own tmp_path.

    Runs for every test. Fixtures that build their own engine (most of the
    router suites do) layer on top of this and win for their own purposes; this
    is the floor, so a path nobody thought about lands in tmp rather than in
    the developer's home directory.
    """
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
# Talking to the API
# --------------------------------------------------------------------------- #
def api_client(app, **kwargs) -> "TestClient":
    """A ``TestClient`` whose base URL already carries the API's prefix.

    Every route now lives under ``API_PREFIX``, because the dashboard is served
    from the same origin and its client-side routes were the same eight URLs as
    the API's. Rather than rewrite several hundred call sites, the prefix goes
    on the client's base URL — which is exactly what the real CLI and the real
    dashboard do, so the tests exercise the same joining the product does.

    The distinction matters for what a *wrong* path now returns: an unmatched
    path outside the prefix is the dashboard's catch-all and answers 200 with
    HTML, so a test that accidentally dropped the prefix would see a puzzling
    success rather than a 404. Going through here is what stops that.
    """
    from fastapi.testclient import TestClient

    from kurukuru.product import API_PREFIX

    # A loopback *host*, for the same reason as the loopback peer below: the
    # service validates the Host header to close DNS rebinding (see
    # docs/SECURITY.md), and Starlette's default of "testserver" is precisely
    # the shape that check exists to refuse. Defaulting to a real loopback name
    # means the tests go through the same path a browser does instead of around
    # it — the alternative, adding "testserver" to the trusted list, would have
    # made every test pass by disabling the thing under test in production too.
    base = kwargs.pop("base_url", "http://127.0.0.1")
    # A loopback peer, because that is what the real service sees: it binds
    # 127.0.0.1 and there is no proxy in front of it. Starlette's default peer
    # is the string "testclient", which is not an address at all — so a route
    # that checks where the request came from (POST /auth/first-run) would
    # refuse every test for a reason no real caller could hit.
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    # And an explicit Host, because `base_url` does not reach every request:
    # `websocket_connect` ignores it and joins against a hard-coded
    # ``ws://testserver`` (see :func:`api_ws`). Without this the console's
    # WebSocket arrives with a Host the service is right to refuse, and every
    # console test fails at the handshake instead of at the thing it tests.
    headers = {"host": "127.0.0.1", **(kwargs.pop("headers", None) or {})}
    return TestClient(
        app, base_url=f"{base.rstrip('/')}{API_PREFIX}", headers=headers, **kwargs
    )


def api_ws(path: str) -> str:
    """A WebSocket path with the API prefix on it.

    Separate from :func:`api_client` because Starlette's
    ``TestClient.websocket_connect`` does **not** use the client's ``base_url``
    — it joins against a hard-coded ``ws://testserver`` — so a WebSocket path
    has to carry the prefix itself even on a client that already has one.

    Worth knowing beyond the tests: the same asymmetry exists in the browser.
    axios has a ``baseURL``; the ``WebSocket`` constructor has nothing of the
    kind, so ``consoleWsUrl`` in the dashboard builds its URL from the prefix
    explicitly too.
    """
    from kurukuru.product import API_PREFIX

    return f"{API_PREFIX}{path}"


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
            "There is no opt-out: no test may touch the real install. Point the "
            "code at tmp_path, or stub the path it treats as real.",
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
