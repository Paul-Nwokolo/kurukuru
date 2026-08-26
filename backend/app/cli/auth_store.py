"""Where the CLI keeps its API token, and how it is protected.

The auth model this phase chose is "always require authentication, and make the
local case frictionless": the token lives in a file the owning OS user can read
and nobody else can, so ``kurukuru`` on your own machine simply already has the
credential. Authentication is always on; there is no trust-by-topology anywhere.

**What the file protection is actually worth**, measured rather than assumed
(see :mod:`app.fs_permissions`): on NTFS it stops other OS users, including
other non-elevated administrators. It does not stop a process running *as you*
— that process could re-grant itself anyway, since owners hold ``WRITE_DAC``.
This is exactly the POSIX ``0600`` boundary, not a weaker one.

So the file is a convenience, not a trust boundary. What is in it is an ordinary
API token: listable, revocable, and dead the moment the password changes. If it
leaks, ``kurukuru auth token rm`` ends it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from app.fs_permissions import describe_protection, harden_file

#: Kept in step with ``Settings.auth_token_file`` by
#: ``test_the_cli_and_the_backend_agree_on_the_token_path``. Duplicated rather
#: than imported because the CLI must not import the backend's settings — see
#: ``test_the_cli_never_reaches_past_the_api`` for why that boundary exists.
DEFAULT_STATE_DIR = "~/.kurukuru"
TOKEN_LEAF = "cli-token"

logger = logging.getLogger("kurukuru.cli.auth")


@dataclass(frozen=True)
class StoredToken:
    token: str
    api_url: str

    @property
    def redacted(self) -> str:
        return f"{self.token[:11]}…" if len(self.token) > 12 else "…"


def token_path(settings=None) -> Path:  # noqa: ANN001 - kept for call-site symmetry
    """Where the token lives, resolved the same way the backend resolves it.

    Honours ``KURUKURU_AUTH_TOKEN_FILE`` first, then ``KURUKURU_STATE_DIR``, then the
    default — the same precedence ``Settings`` applies.
    """
    explicit = os.environ.get("KURUKURU_AUTH_TOKEN_FILE")
    if explicit:
        return Path(explicit).expanduser()
    root = os.environ.get("KURUKURU_STATE_DIR", DEFAULT_STATE_DIR).rstrip("/\\")
    return Path(f"{root}/{TOKEN_LEAF}").expanduser()


def load(settings=None) -> StoredToken | None:
    """The stored token, or None. Never raises for an unreadable file.

    A corrupt or unreadable token file must degrade to "you are not signed in",
    not to a traceback: the fix is ``kurukuru auth login`` either way, and a stack
    trace tells the user nothing they can act on.
    """
    path = token_path(settings)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data["token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token:
        return None
    return StoredToken(token=token, api_url=data.get("api_url", ""))


def save(token: str, api_url: str, settings=None) -> tuple[Path, bool, str]:
    """Write the token and lock it down. Returns (path, protected, detail).

    The caller is expected to *report* the protection rather than assume it. On
    a filesystem with no access control the write still happens — refusing to
    store a revocable token would be a strange place to draw the line — but the
    return says so plainly so the CLI can warn instead of congratulating.
    """
    path = token_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Created empty and hardened *before* the secret goes in, so there is no
    # window in which the token exists in a world-readable file.
    path.touch(mode=0o600, exist_ok=True)
    result = harden_file(path)

    path.write_text(
        json.dumps({"token": token, "api_url": api_url}, indent=2) + "\n",
        encoding="utf-8",
    )
    # Re-asserted after writing: on some filesystems a rewrite can reset
    # inherited permissions, and the second call is a great deal cheaper than
    # finding out it mattered.
    result = harden_file(path)
    logger.debug("Stored CLI token at %s (%s)", path, result.detail)
    return path, result.protected, result.detail


def clear(settings=None) -> bool:
    """Remove the stored token. True if there was one."""
    path = token_path(settings)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("Could not remove %s: %s", path, exc)
        return False


def protection_summary(settings=None) -> str:
    return describe_protection(token_path(settings))
