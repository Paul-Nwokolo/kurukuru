"""
On-disk layout: one root, overridable, with existing installs left alone.

``IAAS_STATE_DIR`` exists so a host can put this tool's state where its
conventions say it belongs — an XDG layout on Linux, a different volume
anywhere — without setting four variables that must agree.

The tests that matter here are the two ways it could do harm: silently moving
an existing install's VMs and keys, or overriding a directory someone chose
deliberately.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import DEFAULT_STATE_DIR, Settings


def test_defaults_are_unchanged_by_the_new_root():
    """An existing install must not notice this setting exists.

    Every one of these paths holds real state — VM disks, the orchestrator's
    keypair, boot media. A default that shifted by one character would strand
    all of it and present as "all my instances disappeared".
    """
    settings = Settings()

    assert settings.state_dir == DEFAULT_STATE_DIR
    assert settings.qemu_dir == "~/.local-iaas/qemu"
    assert settings.ssh_key_dir == "~/.local-iaas/keys"
    assert settings.cloud_init_dir == "~/.local-iaas/cloud-init"
    assert settings.iso_dir == "~/.local-iaas/isos"
    assert settings.db_backup_dir == "~/.local-iaas/backups"
    assert settings.database_url == "sqlite:///~/.local-iaas/iaas.db"


def test_state_dir_moves_every_directory_that_follows_it():
    settings = Settings(state_dir="/var/lib/local-iaas")

    assert settings.qemu_dir == "/var/lib/local-iaas/qemu"
    assert settings.ssh_key_dir == "/var/lib/local-iaas/keys"
    assert settings.cloud_init_dir == "/var/lib/local-iaas/cloud-init"
    assert settings.iso_dir == "/var/lib/local-iaas/isos"
    assert settings.db_backup_dir == "/var/lib/local-iaas/backups"
    assert settings.database_url == "sqlite:////var/lib/local-iaas/iaas.db"


def test_every_rooted_directory_actually_follows_the_root():
    """The guarantee the _ROOTED_DIRS table exists to provide.

    Asserting the table drives the behaviour — rather than listing the
    directories again by hand — is what stops the next directory added to it
    from being silently left behind, which is the exact failure the table was
    introduced to prevent.
    """
    settings = Settings(state_dir="/srv/iaas")
    # Read off the instance, not the class: pydantic exposes a private attr on
    # the class as a ModelPrivateAttr wrapper, and only instance access gives
    # back the dict — which is also how _apply_state_dir reads it.
    rooted = settings._ROOTED_DIRS

    assert rooted, "the table itself must not be empty"
    for field, leaf in rooted.items():
        assert getattr(settings, field) == f"/srv/iaas/{leaf}", field


def test_an_xdg_layout_is_expressible_with_one_variable():
    """The Linux convention, reachable without changing the default for all."""
    settings = Settings(state_dir="~/.local/share/local-iaas")

    assert settings.qemu_dir == "~/.local/share/local-iaas/qemu"
    assert settings.iso_dir == "~/.local/share/local-iaas/isos"


def test_an_explicit_directory_beats_the_root():
    """Naming a directory is a decision; the root must not overrule it.

    The combination is the point: state on the system volume, multi-gigabyte
    boot media somewhere with room.
    """
    settings = Settings(state_dir="/var/lib/local-iaas", iso_dir="/mnt/big/isos")

    assert settings.iso_dir == "/mnt/big/isos"
    assert settings.qemu_dir == "/var/lib/local-iaas/qemu"


def test_a_trailing_separator_does_not_double_up():
    for root in ("/srv/iaas/", "/srv/iaas"):
        assert Settings(state_dir=root).qemu_dir == "/srv/iaas/qemu"


def test_env_vars_drive_it(monkeypatch):
    """The documented interface is the environment, not the constructor."""
    monkeypatch.setenv("IAAS_STATE_DIR", "/opt/iaas")
    settings = Settings()

    assert settings.qemu_dir == "/opt/iaas/qemu"
    assert settings.ssh_key_dir == "/opt/iaas/keys"


# --------------------------------------------------------------------------- #
# The database is a path like any other
# --------------------------------------------------------------------------- #
def test_the_database_is_the_same_file_from_any_working_directory(tmp_path, monkeypatch):
    """The bug this replaced, stated as the property that was missing.

    The default was ``sqlite:///./iaas.db`` — resolved against the process's
    current directory. Started from ``backend/`` you got your instances;
    started from the repo root you got an empty dashboard and a second,
    freshly created database, with the real rows still in the first one.

    Three directories, because two could agree by coincidence — a nested pair
    would both resolve under the same parent if anything went half-right.

    Made **absolute** at each step, which is the whole assertion. Comparing the
    configured paths as written would have passed against the old default too:
    ``Path("iaas.db")`` is the same three-character string from everywhere, and
    it is only the resolution against the process's directory — the one SQLite
    performs, and this test therefore has to perform as well — that differs.
    """
    monkeypatch.setenv("IAAS_STATE_DIR", str(tmp_path / "state"))
    elsewhere = tmp_path / "a" / "deeper" / "place"
    elsewhere.mkdir(parents=True)

    opened = set()
    for directory in (tmp_path, elsewhere, Path(tmp_path.anchor)):
        monkeypatch.chdir(directory)
        path = Settings().database_path
        assert path is not None
        opened.add(Path(os.path.abspath(path)))

    assert len(opened) == 1, f"the database moved with the working directory: {opened}"
    assert opened.pop() == Path(os.path.abspath(tmp_path / "state" / "iaas.db"))


def test_the_database_setting_is_still_overridable(monkeypatch):
    """Including back to a relative path, which is now an explicit decision
    rather than something you get by accident."""
    monkeypatch.setenv("IAAS_DATABASE_URL", "sqlite:///./mine.db")
    assert Settings().database_path == Path("mine.db")

    monkeypatch.setenv("IAAS_DATABASE_URL", "sqlite:////srv/db/iaas.db")
    assert Settings().database_path == Path("/srv/db/iaas.db")


def test_an_explicit_database_beats_the_root():
    """Same rule as every other path: naming it is a decision."""
    settings = Settings(
        state_dir="/var/lib/local-iaas", database_url="sqlite:////mnt/fast/iaas.db"
    )

    assert settings.database_url == "sqlite:////mnt/fast/iaas.db"
    assert settings.qemu_dir == "/var/lib/local-iaas/qemu"


def test_the_home_relative_default_is_expanded_before_it_is_opened():
    """SQLAlchemy does not expand ``~``. Handed the raw URL it would open a
    directory literally named ``~`` inside the current one — which is the same
    CWD-relative bug wearing a different hat."""
    settings = Settings()

    assert "~" in settings.database_url
    assert "~" not in settings.resolved_database_url
    assert settings.resolved_database_url.startswith("sqlite:///")
    assert Path(settings.resolved_database_url.removeprefix("sqlite:///")).is_absolute()


def test_a_server_url_has_no_path_behind_it():
    """``database_path`` is the SQLite accessor, and says so by answering None
    for anything else rather than inventing a filename."""
    assert Settings(database_url="postgresql://localhost/iaas").database_path is None
    assert Settings(database_url="sqlite://").database_path is None


def test_in_memory_databases_are_left_alone():
    """What the test suite itself runs on."""
    settings = Settings(database_url="sqlite://")

    assert settings.resolved_database_url == "sqlite://"


def test_the_default_database_path_tracks_the_state_dir():
    """What the one-time relocation compares against to tell "default layout"
    from "someone named a path"."""
    assert Settings(state_dir="/var/lib/iaas").default_database_path == Path(
        "/var/lib/iaas/iaas.db"
    )
    assert (
        Settings(state_dir="/var/lib/iaas", database_url="sqlite:////elsewhere.db")
        .default_database_path
        != Settings(state_dir="/var/lib/iaas", database_url="sqlite:////elsewhere.db")
        .database_path
    )


def test_the_state_dir_is_where_the_database_lands(monkeypatch):
    """The whole point of the change, in one line: one variable moves the lot,
    database included."""
    monkeypatch.setenv("IAAS_STATE_DIR", os.path.join(os.sep, "opt", "iaas"))
    settings = Settings()

    assert settings.database_path == Path(os.sep) / "opt" / "iaas" / "iaas.db"
    assert settings.database_path.parent == Path(settings.state_dir).expanduser()
