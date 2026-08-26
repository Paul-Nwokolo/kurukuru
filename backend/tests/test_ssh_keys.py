"""
Unit tests for the orchestrator SSH key manager.

``subprocess.run`` is mocked so ssh-keygen is never actually invoked. The mock
simulates key creation by writing placeholder files, letting us assert
idempotency (existing keypair reused, ssh-keygen NOT called again) and the
error paths (missing binary, non-zero exit).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from kurukuru.config import Settings
from kurukuru.ssh_keys import (
    SSHKeyError,
    ensure_keypair,
    get_public_key,
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(ssh_key_dir=str(tmp_path / "keys"))


def _fake_keygen_factory(pub_contents: str = "ssh-ed25519 AAAAFAKE test"):
    """Return a subprocess.run side-effect that 'creates' the keypair files."""

    def _run(cmd, **_kwargs):
        # cmd = [ssh-keygen, -t, ed25519, -N, "", -f, <priv>, -C, <comment>]
        priv = Path(cmd[cmd.index("-f") + 1])
        priv.parent.mkdir(parents=True, exist_ok=True)
        priv.write_text("PRIVATE-KEY-MATERIAL", encoding="utf-8")
        priv.with_suffix(".pub").write_text(pub_contents, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    return _run


def test_generates_keypair_when_absent(settings):
    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=_fake_keygen_factory()) as run:
        paths = ensure_keypair(settings)

    assert paths.private.exists()
    assert paths.public.exists()
    assert run.call_count == 1
    # sanity: invoked ed25519, non-interactive (empty passphrase)
    args = run.call_args.args[0]
    assert "ed25519" in args and "-N" in args


def test_idempotent_reuse_does_not_regenerate(settings):
    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=_fake_keygen_factory()) as run:
        first = ensure_keypair(settings)
        second = ensure_keypair(settings)

    assert first == second
    # ssh-keygen called exactly once — the second call reused the keypair.
    assert run.call_count == 1


def test_get_public_key_returns_contents(settings):
    key = "ssh-ed25519 AAAAC3TestKey local-iaas-orchestrator"
    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=_fake_keygen_factory(key)):
        assert get_public_key(settings) == key


def test_missing_binary_raises(settings):
    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(SSHKeyError):
            ensure_keypair(settings)


def test_nonzero_exit_raises(settings):
    def _fail(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")

    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=_fail):
        with pytest.raises(SSHKeyError):
            ensure_keypair(settings)


def test_inconsistent_pair_raises(settings):
    # Only the private half exists -> inconsistent, must not silently overwrite.
    keydir = Path(settings.ssh_key_dir)
    keydir.mkdir(parents=True, exist_ok=True)
    (keydir / "id_ed25519").write_text("orphan private", encoding="utf-8")

    with patch("kurukuru.ssh_keys.subprocess.run", side_effect=_fake_keygen_factory()):
        with pytest.raises(SSHKeyError):
            ensure_keypair(settings)
