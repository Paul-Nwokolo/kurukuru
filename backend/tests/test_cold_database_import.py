"""``kurukuru auth init`` from a process that has only imported the CLI.

This file exists for one bug, and the bug's shape is the reason the rest of
the suite could not see it.

``kurukuru.database`` builds its engine lazily through PEP 562's module-level
``__getattr__``. That serves ``kurukuru.database.engine`` to *importers*. It
does not serve a bare ``engine`` written inside a function in that same file:
such a name compiles to a global lookup, and global lookup never consults
``__getattr__``. It only ever worked because somebody had already fetched the
attribute, which caches it into the module's globals and makes the bare name
resolve from then on.

So ``init_db()`` was depending on an importer it never named. In the backend
that dependency is always satisfied — the routers import the engine at startup.
On the CLI's ``auth init`` path it is not, and the first command a new user
runs died with ``NameError: name 'engine' is not defined``.

**Every existing test satisfies the dependency by accident**: they import
routers, or the app, or patch ``kurukuru.database.engine`` directly, and any
one of those populates the global before anything calls ``init_db``. So the
test has to be run somewhere that has imported nothing — which means a
subprocess, the same device ``test_isolation.py`` uses for the same reason.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


def _in_a_cold_process(body: str, state_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a fresh interpreter whose only import is the one it makes.

    ``-S`` is deliberately *not* used — site is needed for the venv — but
    nothing else is imported for it, which is the whole point: the moment this
    process imports a router, the bug disappears.
    """
    return subprocess.run(
        [sys.executable, "-c", body],
        capture_output=True,
        text=True,
        cwd=BACKEND,
        timeout=120,
        env={
            **_clean_env(),
            "KURUKURU_STATE_DIR": str(state_dir),
        },
    )


def _clean_env() -> dict[str, str]:
    import os

    # Everything except this product's own variables, so the child is
    # configured by the one setting passed in and not by whatever the
    # developer has exported.
    return {k: v for k, v in os.environ.items() if not k.startswith("KURUKURU_")}


def test_init_db_works_in_a_process_that_imported_only_the_database(tmp_path):
    """The failure, reproduced at its narrowest.

    Before the fix this raised ``NameError: name 'engine' is not defined``.
    """
    result = _in_a_cold_process(
        "import kurukuru.database as database\n"
        "database.init_db()\n"
        "print('OK')\n",
        tmp_path / "state",
    )

    assert "NameError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_the_cli_can_check_for_an_account_without_the_backend_imported(tmp_path):
    """The real path: ``auth init`` asks this before prompting for anything.

    ``host_admin`` is the CLI's one sanctioned reach past the API, and it is
    the caller that had nothing to populate the global for it.
    """
    result = _in_a_cold_process(
        "from kurukuru.cli import host_admin\n"
        "print('exists:', host_admin.account_exists())\n",
        tmp_path / "state",
    )

    assert "NameError" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "exists: False" in result.stdout


def test_a_patched_engine_still_wins(tmp_path, monkeypatch):
    """The accessor must not defeat the isolation the whole suite depends on.

    ``conftest.isolated_state`` patches ``kurukuru.database.engine``; if the
    internal accessor ignored that and built its own from settings, every test
    would quietly start talking to the real database.
    """
    from sqlmodel import SQLModel, create_engine

    import kurukuru.database as database

    sentinel = create_engine("sqlite://")
    SQLModel.metadata.create_all(sentinel)
    monkeypatch.setattr(database, "engine", sentinel)

    assert database._engine_instance() is sentinel
