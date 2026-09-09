"""Locking a secret to its owner, and refusing to claim it when we cannot.

The whole point of this module is that it does not lie. ``os.chmod`` on NTFS
returns success and changes nothing about who can read the file — measured, the
DACL is byte-identical — so a hardening routine that reports "done" because a
call did not raise is worse than none: it produces a token file everyone can
read and a log line saying it is safe.

So the assertions here are mostly about the *reporting* being honest, not about
the mechanism working on a good day.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kurukuru.fs_permissions import (
    HardenResult,
    PermissionHardeningError,
    describe_protection,
    harden_file,
)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL behaviour")


@pytest.fixture()
def secret(tmp_path: Path) -> Path:
    path = tmp_path / "api.token"
    path.write_text("t0k3n")
    yield path
    if sys.platform == "win32" and path.exists():
        # Give the ACL back so pytest's tmp_path cleanup can remove it.
        subprocess.run(["icacls", str(path), "/grant", f"{os.environ['USERNAME']}:(F)"],
                       capture_output=True)


def test_hardening_leaves_the_file_usable_by_its_owner(secret):
    """A protected secret nobody can read is not a feature."""
    result = harden_file(secret)

    assert result.protected, result.detail
    assert secret.read_text() == "t0k3n"
    secret.write_text("rotated")
    assert secret.read_text() == "rotated"


def test_the_result_is_truthy_only_when_the_os_enforced_something():
    assert bool(HardenResult(Path("x"), True, "ok")) is True
    assert bool(HardenResult(Path("x"), False, "no acls here")) is False


@windows_only
def test_hardening_removes_the_inherited_entries(secret):
    """Inheritance under the user profile grants SYSTEM and Administrators.
    Leaving them is a weaker protection than the one being reported."""
    harden_file(secret)

    described = describe_protection(secret)

    # Reported in full on failure. This used to be three bare `in` checks
    # against a long string, so pytest elided the middle — the CI failure read
    # `'Administrators' not in 'C:\Users\...R RIGHTS:(F)'`, which names the
    # problem and hides the evidence. Now whatever fails prints the whole ACL.
    for principal in ("Administrators", "SYSTEM"):
        assert principal not in described, (
            f"{principal} still has access after hardening. "
            f"  full ACL: {described}"
        )
    assert os.environ["USERNAME"] in described, f"full ACL: {described}"


@windows_only
def test_a_filesystem_without_acls_is_reported_as_unprotected(secret, monkeypatch):
    """The case the brief asked for. icacls reports success on some of these,
    so the filesystem is checked *before* believing the command."""
    monkeypatch.setattr("kurukuru.fs_permissions._windows_filesystem", lambda p: "FAT32")

    result = harden_file(secret)

    assert not result.protected
    assert "FAT32" in result.detail
    assert "cannot be restricted" in result.detail
    # And it names the way out rather than only the problem.
    assert "KURUKURU_STATE_DIR" in result.detail


@windows_only
def test_strict_mode_raises_rather_than_writing_a_secret_in_the_open(secret, monkeypatch):
    monkeypatch.setattr("kurukuru.fs_permissions._windows_filesystem", lambda p: "exFAT")

    with pytest.raises(PermissionHardeningError) as excinfo:
        harden_file(secret, strict=True)

    assert "exFAT" in str(excinfo.value)


@windows_only
def test_a_failed_grant_is_not_reported_as_success(secret, monkeypatch):
    def failing(*args, **kwargs):
        return subprocess.CompletedProcess(args, 5, "", "Access is denied.")

    monkeypatch.setattr("kurukuru.fs_permissions._icacls", failing)

    result = harden_file(secret)

    assert not result.protected
    assert "Access is denied" in result.detail


@windows_only
def test_chmod_alone_would_not_have_protected_it(secret):
    """The measurement this module exists because of, pinned as a test.

    If a future refactor 'simplifies' harden_file back to os.chmod, this fails.
    """
    before = describe_protection(secret)
    os.chmod(secret, 0o600)
    assert describe_protection(secret) == before, (
        "chmod changed the DACL on this platform — the premise of this module "
        "has changed and its comments need revisiting"
    )

    harden_file(secret)
    assert describe_protection(secret) != before


def test_describe_protection_handles_a_missing_file(tmp_path):
    assert "does not exist" in describe_protection(tmp_path / "nope")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_posix_hardening_sets_0600(secret):
    result = harden_file(secret)

    assert result.protected
    assert secret.stat().st_mode & 0o777 == 0o600


@windows_only
def test_hardening_removes_explicit_entries_not_only_inherited(secret):
    """The bug CI found, reproduced as an assertion.

    `/inheritance:r` removes inherited ACEs and leaves explicit ones alone. On
    an administrator account a new file gets SYSTEM, Administrators and OWNER
    RIGHTS as *explicit* entries from the token's default DACL, so the old code
    returned True and reported "restricted to <SID>" over a file three other
    principals held Full Control on.

    The starting DACL here is the one the GitHub runner produced, rebuilt by
    hand so the case is reproducible on an ordinary non-admin account too —
    this test failed before the fix on the author's own machine, which is the
    only reason it is worth having.
    """
    import kurukuru.fs_permissions as fsp

    subprocess.run(["icacls", str(secret), "/inheritance:r"], capture_output=True)
    subprocess.run(
        ["icacls", str(secret), "/grant:r",
         "*S-1-5-18:(F)", "*S-1-5-32-544:(F)", "*S-1-3-4:(F)",
         f"{os.environ['USERNAME']}:(R,W)"],
        capture_output=True,
    )
    assert len(fsp._dacl_principals(secret)) > 1, "the starting state did not set up"

    result = harden_file(secret)

    remaining = fsp._dacl_principals(secret)
    assert bool(result), result.detail
    assert len(remaining) == 1, f"entries survived hardening: {remaining}"
    assert secret.read_text() == "t0k3n", "the owner can no longer read its own secret"


@windows_only
def test_hardening_refuses_to_claim_success_when_entries_survive(secret, monkeypatch):
    """Proves the read-back guard fires, by breaking the removal on purpose.

    Without this the new check is a claim: it has never been observed turning a
    DACL it could not clean into a falsy result. Removal is stubbed to a no-op,
    which is exactly what the old code effectively did for explicit entries.
    """
    import kurukuru.fs_permissions as fsp

    real = fsp._icacls

    def refuse_to_remove(*args: str):
        if "/remove:g" in args:
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(*args)

    monkeypatch.setattr(fsp, "_icacls", refuse_to_remove)
    subprocess.run(["icacls", str(secret), "/grant", "*S-1-5-32-544:(F)"], capture_output=True)

    result = harden_file(secret)

    assert not result, "hardening claimed success over a DACL it did not clean"
    assert "entries remain" in result.detail
