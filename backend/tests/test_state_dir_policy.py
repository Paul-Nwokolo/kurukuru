"""Where state lives when nothing is configured — the policy, on every platform.

``default_state_dir`` takes the platform, the environment and the home
directory as parameters, so every branch of the Linux policy runs on the
Windows dev machine and every Windows branch runs on the Linux CI job. A policy
only ever exercised on the platform it was written on is how Phase 9 found a
WHPX assumption in every corner of the accelerator code.

The cases that matter are the ones that could cost someone their VMs: an
existing ``~/.kurukuru`` on Linux must keep being used where it is, and nothing
here may ever choose a path that splits one install across two trees.
DECISIONS #69.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kurukuru.product import STATE_DIR, default_config_dir, default_state_dir


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_windows_and_macos_keep_the_dotfile_tree(tmp_path: Path, platform: str):
    assert default_state_dir(platform, {}, tmp_path) == STATE_DIR == "~/.kurukuru"
    assert default_config_dir(platform, {}, tmp_path) == STATE_DIR


def test_xdg_on_a_new_linux_install(tmp_path: Path):
    assert default_state_dir("linux", {}, tmp_path) == "~/.local/share/kurukuru"
    assert default_config_dir("linux", {}, tmp_path) == "~/.config/kurukuru"


def test_the_xdg_variables_are_honoured(tmp_path: Path):
    env = {"XDG_DATA_HOME": "/srv/data/", "XDG_CONFIG_HOME": "/srv/config"}

    assert default_state_dir("linux", env, tmp_path) == "/srv/data/kurukuru"
    assert default_config_dir("linux", env, tmp_path) == "/srv/config/kurukuru"


@pytest.mark.parametrize("value", ["relative/data", "", "~/data"])
def test_a_non_absolute_xdg_value_is_ignored(tmp_path: Path, value: str):
    """The spec says so, and the reason is concrete: resolved against the
    working directory, a relative value puts VMs wherever the shell was when
    the service started."""
    env = {"XDG_DATA_HOME": value, "XDG_CONFIG_HOME": value}

    assert default_state_dir("linux", env, tmp_path) == "~/.local/share/kurukuru"
    assert default_config_dir("linux", env, tmp_path) == "~/.config/kurukuru"


def test_an_existing_dotfile_tree_on_linux_keeps_winning(tmp_path: Path):
    """The case this whole policy is shaped around. Somebody ran a checkout on
    Linux before 0.1.5; their VMs are in ~/.kurukuru. It stays where it is —
    even with XDG variables set, which would otherwise point elsewhere — and
    config stays beside it, one tree as before."""
    (tmp_path / ".kurukuru").mkdir()
    env = {"XDG_DATA_HOME": "/srv/data", "XDG_CONFIG_HOME": "/srv/config"}

    assert default_state_dir("linux", env, tmp_path) == STATE_DIR
    assert default_config_dir("linux", env, tmp_path) == STATE_DIR


def test_an_existing_tree_is_never_moved_by_resolving(tmp_path: Path):
    """Resolving is a read. Nothing is created, moved or touched."""
    legacy = tmp_path / ".kurukuru"
    legacy.mkdir()
    (legacy / "kurukuru.db").write_bytes(b"precious")
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    default_state_dir("linux", {}, tmp_path)
    default_config_dir("linux", {}, tmp_path)

    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == before
    assert (legacy / "kurukuru.db").read_bytes() == b"precious"
    assert not (tmp_path / ".local").exists()


def test_the_cli_and_the_backend_resolve_the_same_tree():
    """The CLI's token and config paths used to be literals of their own.
    On Linux that would have put the CLI's token in ~/.kurukuru and the
    backend's in ~/.local/share/kurukuru — two installs, one each."""
    from kurukuru.cli import auth_store
    from kurukuru.cli.naming import config_path
    from kurukuru.config import CONFIG_FILE, DEFAULT_STATE_DIR
    from kurukuru.product import default_config_dir

    assert auth_store.DEFAULT_STATE_DIR == DEFAULT_STATE_DIR
    # Compared as the policy's answers, at call time: the CLI's config path is
    # resolved when the file is opened (naming.config_path), so it follows the
    # sandboxed home; CONFIG_FILE was expanded at import, before the sandbox.
    assert config_path() == f"{default_config_dir()}/cli.toml"
    assert CONFIG_FILE.name == "kurukuru.env"
    assert CONFIG_FILE.parent.name == Path(default_config_dir()).name


def test_the_cli_reads_its_config_from_where_the_policy_says_now(monkeypatch, tmp_path):
    """The Linux CI finding: with XDG_CONFIG_HOME set when the module was
    imported, an import-time copy of cli.toml's path pointed at the real
    ~/.config. The loader must ask the policy at the moment it opens the file."""
    from kurukuru.cli import config as cli_config
    from kurukuru.product import default_config_dir

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    expected = Path(f"{default_config_dir()}/cli.toml").expanduser()

    assert cli_config.config_file_path() == expected
