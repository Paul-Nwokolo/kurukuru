"""
Unit tests for cloud-init generation.

The SSH public key is injected via a patched ``get_public_key`` so these tests
never invoke ssh-keygen or touch the real keypair. We render the YAML, parse it
back with ``yaml.safe_load``, and assert on structure — proving the document is
valid cloud-config, not just a string that looks right.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import app.cloud_init as cloud_init
from app.cloud_init import CloudInitError, build_cloud_init, build_config, cleanup
from app.config import Settings

FAKE_KEY = "ssh-ed25519 AAAAC3FakeKeyForTests test@orchestrator"


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(cloud_init_dir=str(tmp_path / "cloud-init"), default_vm_user="iaas")


@pytest.fixture(autouse=True)
def _patch_pubkey(monkeypatch):
    monkeypatch.setattr(cloud_init, "get_public_key", lambda *_a, **_k: FAKE_KEY)


def test_build_config_structure(settings):
    cfg = build_config("web-1", settings)

    assert cfg["package_update"] is True
    assert cfg["packages"] == ["curl", "htop"]
    assert len(cfg["users"]) == 1
    user = cfg["users"][0]
    assert user["name"] == "iaas"
    assert user["sudo"] == "ALL=(ALL) NOPASSWD:ALL"
    assert user["shell"] == "/bin/bash"
    assert user["ssh_authorized_keys"] == [FAKE_KEY]


def test_build_config_respects_custom_user(tmp_path):
    s = Settings(cloud_init_dir=str(tmp_path), default_vm_user="operator")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cloud_init, "get_public_key", lambda *_a, **_k: FAKE_KEY)
        cfg = build_config("x", s)
    assert cfg["users"][0]["name"] == "operator"


def test_build_cloud_init_writes_valid_yaml_with_header(settings):
    path = build_cloud_init("cloudinit-test", settings)

    assert path.exists()
    assert path.name == "cloudinit-test.yaml"

    raw = path.read_text(encoding="utf-8")
    # cloud-init requires the header on the first line.
    assert raw.splitlines()[0] == "#cloud-config"

    # Parses back to the same structure.
    parsed = yaml.safe_load(raw)
    assert parsed["package_update"] is True
    assert parsed["packages"] == ["curl", "htop"]
    assert parsed["users"][0]["ssh_authorized_keys"] == [FAKE_KEY]
    assert parsed["users"][0]["name"] == "iaas"


def test_generated_cloud_init_uses_unix_line_endings(settings):
    """A guest artifact must not pick up the host's line endings.

    Written in text mode this file is CRLF on Windows and LF on Linux — two
    different documents from one input. It reaches cloud-init intact today only
    because the engine reads it back through universal newlines on the way into
    the seed ISO, so the defect is cancelled rather than absent; a switch to
    read_bytes would deliver CRLF user-data to the guest. Asserting on the
    bytes is what keeps that from being discovered in a guest that won't boot.
    """
    raw = build_cloud_init("line-endings", settings).read_bytes()

    assert b"\r" not in raw
    assert raw.startswith(b"#cloud-config\n")


def test_cleanup_removes_file_and_is_idempotent(settings):
    path = build_cloud_init("gone", settings)
    assert path.exists()
    cleanup(path)
    assert not path.exists()
    # Second cleanup on a missing file must not raise.
    cleanup(path)
    # None is a safe no-op.
    cleanup(None)


def test_build_config_propagates_key_errors_as_cloud_init_error(settings, monkeypatch):
    from app.ssh_keys import SSHKeyError

    def boom(*_a, **_k):
        raise SSHKeyError("ssh-keygen missing")

    monkeypatch.setattr(cloud_init, "get_public_key", boom)
    with pytest.raises(CloudInitError):
        build_config("x", settings)
