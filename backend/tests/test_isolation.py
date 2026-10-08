"""
Tests for the test harness itself.

Two incidents motivated this file. Phase 10's ``POST /keypairs/generate`` ran
real ``ssh-keygen`` into the developer's ``~/.kurukuru/keys`` because one
fixture pinned the ISO directory and not the key directory. Phase 11's event
writer put 130 rows into the real ``iaas.db`` because one of four test modules
was never added to a hand-maintained list of modules to redirect. Both were
found by a human noticing something odd in the live dashboard, weeks and one
phase apart respectively.

So the harness is now load-bearing, and load-bearing things get tests. What is
asserted here:

* the redirect actually reaches **every** module, discovered not listed — the
  assertion that would have caught the second incident;
* nothing a test can write lands on a real path — the first;
* and the guard genuinely fails a run, proven by running a deliberately
  leaking test in a subprocess and reading the exit code, because a guard
  nobody has ever seen fire is a guard nobody knows works.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.conftest import (
    REAL_DB_FILES,
    REAL_STATE_DIR,
    _app_modules_with,
    _fingerprint,
    _real_state_fingerprint,
)

BACKEND = Path(__file__).resolve().parent.parent
REAL_DB = REAL_DB_FILES[0]


# --------------------------------------------------------------------------- #
# The redirect reaches everything
# --------------------------------------------------------------------------- #
def test_every_module_that_holds_an_engine_got_the_test_one(tmp_path):
    """The assertion that would have caught the event-log leak.

    ``from kurukuru.database import engine as db_engine`` binds a copy, so six
    modules each hold their own reference. Five were redirected; the sixth was
    new. Nothing in the failing run said so — the tests passed, and the rows
    turned up in the real database.
    """
    modules = _app_modules_with("db_engine")

    assert modules, "no module holds db_engine — has the import style changed?"
    for module in modules:
        url = module.db_engine.url
        # Compared as a *path*, against the one place the real database is.
        # This used to look for the backend directory in the URL string, which
        # stopped meaning anything the moment the database moved out of it —
        # the assertion would have gone on passing while measuring nothing.
        database = Path(url.database) if url.database else None
        assert database != REAL_DB, (
            f"{module.__name__} still points at the real database: {url}"
        )


def test_every_module_that_holds_settings_got_the_test_ones(tmp_path):
    """Same reasoning, for the half of it that writes files rather than rows."""
    modules = _app_modules_with("get_settings")

    assert modules
    for module in modules:
        state = Path(module.get_settings().state_dir).expanduser()
        assert state != REAL_STATE_DIR, (
            f"{module.__name__} still resolves the real state directory"
        )


def test_the_settings_a_test_sees_are_rooted_in_its_own_tmp_path(
    isolated_state, tmp_path
):
    settings = isolated_state

    for directory in (
        settings.state_dir,
        settings.ssh_key_dir,
        settings.qemu_dir,
        settings.iso_dir,
        settings.cloud_init_dir,
    ):
        assert Path(directory).is_relative_to(tmp_path)
    assert str(tmp_path.as_posix()) in settings.database_url


def test_generating_a_keypair_writes_into_tmp_not_the_real_key_directory(tmp_path):
    """Phase 10's leak, reproduced as an assertion.

    This runs real ssh-keygen and really writes two files, which is exactly why
    it was able to litter a developer's home directory ten times over.
    """
    from kurukuru.config import get_settings
    from kurukuru.keypairs import generate_keypair

    private, _ = generate_keypair("harness check", get_settings())

    assert private.is_relative_to(tmp_path)
    assert not private.is_relative_to(REAL_STATE_DIR)


# --------------------------------------------------------------------------- #
# The guard detects
# --------------------------------------------------------------------------- #
def test_the_fingerprint_notices_a_modified_file(tmp_path):
    """A SQLite write lands in the WAL sidecar first, so watching the .db file
    alone would miss it — hence size+mtime on all three.

    Exercised against a temporary tree rather than the paths it really watches:
    proving the detector by writing into a live database's WAL is a good way to
    corrupt the thing it exists to protect.
    """
    watched = tmp_path / "iaas.db-wal"
    watched.write_bytes(b"one")
    before = _fingerprint([watched], [])

    watched.write_bytes(b"two words")

    assert _fingerprint([watched], []) != before


def test_the_fingerprint_notices_a_file_appearing(tmp_path):
    """Phase 10's leak was a *new file*, not a modified one."""
    absent = tmp_path / "iaas.db"
    before = _fingerprint([absent], [])

    absent.write_text("now it exists")

    assert _fingerprint([absent], []) != before


def test_the_fingerprint_notices_a_new_entry_in_a_watched_directory(tmp_path):
    """How a stray keypair shows up: the directory gains a name."""
    keys = tmp_path / "keys"
    keys.mkdir()
    before = _fingerprint([], [keys])

    (keys / "stray-key").write_text("leaked")

    assert _fingerprint([], [keys]) != before


def test_a_file_created_and_removed_within_a_test_is_still_noticed(tmp_path):
    """The shape the listing alone missed: a stray file that is gone again by
    the time the guard looks. The directory's mtime remembers it."""
    from tests.conftest import _quiescent_mtimes

    keys = tmp_path / "keys"
    keys.mkdir()
    before = _quiescent_mtimes([keys])
    time.sleep(0.05)  # a filesystem timestamp needs a tick to move

    stray = keys / "leaked-by-test.tmp"
    stray.write_text("x")
    stray.unlink()

    assert _quiescent_mtimes([keys]) != before


def test_the_fingerprint_is_stable_when_nothing_changes(tmp_path):
    """The control: a guard that fires on every test is noise, not a guard."""
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "expected").write_text("x")

    assert _fingerprint([keys / "expected"], [keys]) == _fingerprint(
        [keys / "expected"], [keys]
    )


def test_the_guard_watches_the_key_directory_that_actually_leaked(tmp_path):
    """Wiring, asserted without touching anything: the watch list is a pure
    function, so this reads no real directory at all."""
    from tests.conftest import REAL_HOME, _real_state_watch_list

    watched = set(_real_state_watch_list())

    assert REAL_STATE_DIR / "keys" in watched
    assert REAL_STATE_DIR in watched
    assert REAL_HOME / ".config" / "systemd" / "user" in watched


def test_opening_the_real_database_raises_immediately(tmp_path):
    """The database half of the guard: prevention, not detection.

    mtime cannot tell "this test wrote" from "the developer's backend wrote" —
    its reconciler runs every 30 seconds — so watching the file blamed
    whichever test straddled a reconcile pass. This catches the offender in the
    act instead, and cannot be confused by another process.
    """
    import sqlite3

    import tests.conftest as harness

    # Against a stand-in for the real database, not the real one: if the guard
    # were ever broken, the old form of this test would itself have opened the
    # developer's live kurukuru.db. The guard reads its protected path at call
    # time, so pointing it here exercises exactly the same code.
    stand_in = (tmp_path / "kurukuru.db").resolve()
    original = harness._REAL_DB
    harness._REAL_DB = stand_in
    try:
        with pytest.raises(RuntimeError, match="real database"):
            sqlite3.connect(str(stand_in))
    finally:
        harness._REAL_DB = original
    assert not stand_in.exists(), "the guard let the connection through"


def test_an_engine_built_against_the_real_url_cannot_connect(tmp_path):
    """The realistic shape of the mistake: not a raw sqlite3 call but a fixture
    that builds its own engine and forgets to point it somewhere safe."""
    from sqlmodel import Session, create_engine

    import tests.conftest as harness
    from kurukuru.config import Settings

    # `resolved_database_url`, which is what kurukuru.database itself opens: the
    # configured URL still holds an unexpanded "~", and SQLAlchemy would take
    # that literally and miss the real file entirely. Built against a stand-in
    # state directory under tmp_path, with the guard pointed at its database —
    # a stubbed "real database" — so a broken guard could only ever open a
    # throwaway file, never the developer's live one.
    settings = Settings(state_dir=str(tmp_path / "state"))
    protected = settings.database_path.resolve()
    protected.parent.mkdir(parents=True)
    engine = create_engine(settings.resolved_database_url)
    original = harness._REAL_DB
    harness._REAL_DB = protected
    try:
        with pytest.raises(RuntimeError, match="real database"):
            with Session(engine) as session:
                session.exec(__import__("sqlmodel").text("select 1"))
    finally:
        harness._REAL_DB = original
        engine.dispose()
    assert protected.is_relative_to(tmp_path.resolve())
    assert not protected.exists(), "the guard let the engine connect"


# --------------------------------------------------------------------------- #
# The guard fails the run
# --------------------------------------------------------------------------- #
def _run_pytest(
    tmp_path: Path,
    body: str,
    *,
    home: Path,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run one generated test under this suite's conftest, in a subprocess.

    A subprocess because the thing being tested is a fixture that fails the
    test it wraps — it cannot be exercised in-process without failing this one.
    """
    # ``home`` is required, not optional: a generated test that forgot it would
    # resolve the developer's real state tree. See _fake_home.
    env = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(BACKEND)!r})
            from tests.conftest import *  # noqa: F403
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "test_generated.py").write_text(textwrap.dedent(body), encoding="utf-8")

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            # Pin the rootdir to the generated directory. Without it pytest
            # infers one by walking upwards and can end up scanning the drive
            # root — on a GitHub Windows runner that means `C:\Documents and
            # Settings`, an inaccessible junction, and collection dies with
            # PermissionError before the generated test ever runs. The failure
            # looked like this harness being broken; it was pytest looking
            # somewhere this test never meant to point it.
            "--rootdir",
            str(tmp_path),
            *(extra_args or []),
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND,
        env=env,
        timeout=300,
    )


def _fake_home(tmp_path: Path) -> Path:
    """A home directory for the subprocess, with a state tree inside it.

    The two tests below prove the guard by letting a generated test write into
    "the real state directory" and watching the run go red. They used to do
    that against the developer's actual ``~/.kurukuru/keys`` — creating a file
    there, deleting it after, and on a machine with no install creating and
    removing the directory itself. Harmless every time it worked, and the one
    directory in this project that has already been lost once (see
    ``docs/DECISIONS.md`` on the state directory). Phase 17 found it by
    watching that directory's mtime move on every full run.

    So the subprocess is given its own home. ``tests/conftest.py`` computes
    ``REAL_STATE_DIR`` as ``~/.kurukuru`` expanded *when it is imported*, and
    ``expanduser`` reads ``USERPROFILE`` on Windows and ``HOME`` elsewhere — so
    in a subprocess started with both pointing here, "the real state" the guard
    watches *is* this tree. The guard code under test is unchanged; only what
    it is aimed at moved.
    """
    home = tmp_path / "home"
    (home / ".kurukuru" / "keys").mkdir(parents=True)
    return home


def _fake_keys(home: Path) -> Path:
    return home / ".kurukuru" / "keys"


def test_the_fake_home_is_what_the_subprocess_calls_real(tmp_path):
    """Guards the two tests below: if the redirect stopped working, they would
    go back to writing into the developer's real state tree while still
    passing. This asserts where the subprocess's conftest actually looks."""
    home = _fake_home(tmp_path)
    result = _run_pytest(
        tmp_path,
        """
        from tests.conftest import REAL_STATE_DIR

        def test_where():
            print("REAL_STATE_DIR=" + str(REAL_STATE_DIR))
        """,
        home=home,
        extra_args=["-s"],
    )

    assert result.returncode == 0, result.stdout
    assert f"REAL_STATE_DIR={home / '.kurukuru'}" in result.stdout
    assert str(REAL_STATE_DIR) not in result.stdout


def test_a_test_that_writes_to_the_real_state_fails_the_run(tmp_path):
    """The whole point, end to end.

    Without this, the guard is a claim. With it, the guard has been observed
    turning a leak into a red test naming the file that caused it — against a
    state tree that is "real" to the subprocess and nobody's actual install.
    The generated test is not marked: it is what the guard has to catch,
    running under its own, separate, non-exempt pytest process.
    """
    home = _fake_home(tmp_path)
    result = _run_pytest(
        tmp_path,
        """
        from tests.conftest import REAL_STATE_DIR

        def test_leaks():
            stray = REAL_STATE_DIR / "keys" / "leaked-by-test.tmp"
            stray.write_text("this should fail the run")
            # Deliberately not cleaned up: the guard is what must notice.
        """,
        home=home,
    )

    assert result.returncode != 0, result.stdout
    assert "wrote to the real install" in result.stdout
    assert "test_leaks" in result.stdout
    # And it landed where the redirect said, which is the evidence that the
    # developer's own tree was never the target.
    assert (_fake_keys(home) / "leaked-by-test.tmp").exists()


def test_a_test_that_writes_a_unit_into_the_real_home_fails_the_run(tmp_path):
    """The Phase 18 escape, end to end: a test that wrote a systemd unit into
    the developer's real ~/.config/systemd/user, creating the directories on
    the way. The guard watched only the state tree and said nothing; it now
    watches the home locations the product writes to on Linux. Run against the
    subprocess's fake home, like the leak test above."""
    home = _fake_home(tmp_path)
    result = _run_pytest(
        tmp_path,
        """
        from tests.conftest import REAL_HOME

        def test_escapes_to_home():
            units = REAL_HOME / ".config" / "systemd" / "user"
            units.mkdir(parents=True)
            (units / "kurukuru.service").write_text("[Service]")
        """,
        home=home,
    )

    assert result.returncode != 0, result.stdout
    assert "wrote to the real install" in result.stdout
    assert "systemd" in result.stdout
    assert (home / ".config" / "systemd" / "user" / "kurukuru.service").exists()


def test_an_ordinary_test_passes_under_the_same_harness(tmp_path):
    """The control. A guard that fails everything proves nothing."""
    result = _run_pytest(
        tmp_path,
        """
        def test_writes_nowhere_real(tmp_path):
            (tmp_path / "fine.txt").write_text("ok")
        """,
        home=_fake_home(tmp_path),
    )

    assert result.returncode == 0, result.stdout


def test_there_is_no_way_to_opt_out_of_the_guard(tmp_path):
    """The ``real_state`` marker used to switch every guard off for a test, and
    it is what let the old self-tests write into the developer's real keys
    directory unnoticed. It was removed rather than documented; with
    ``--strict-markers`` a test that asks for it fails before it runs.

    Run with ``--strict-markers`` passed explicitly: the generated test's
    rootdir is tmp_path, so the backend's pyproject (where the option is set
    for the real suite) is not read here."""
    home = _fake_home(tmp_path)
    result = _run_pytest(
        tmp_path,
        """
        import pytest
        from tests.conftest import REAL_STATE_DIR

        @pytest.mark.real_state
        def test_asks_to_touch_it():
            stray = REAL_STATE_DIR / "keys" / "marked-opt-in.tmp"
            stray.write_text("not permitted")
        """,
        home=home,
        extra_args=["--strict-markers"],
    )

    assert result.returncode != 0, result.stdout
    assert "real_state" in result.stdout and "not found in `markers`" in result.stdout
    assert not (home / ".kurukuru" / "keys" / "marked-opt-in.tmp").exists()
