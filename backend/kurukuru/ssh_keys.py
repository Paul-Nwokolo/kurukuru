"""
Orchestrator SSH key management.

The orchestrator owns a single dedicated ed25519 keypair. Its public half is
injected into every VM (via cloud-init) so the operator can immediately
``ssh <user>@<ip>`` using the matching private key. The keypair is generated
lazily on first use with the system ``ssh-keygen`` and then reused forever —
it is never regenerated or overwritten.

Only this module knows how keys are created and where they live; callers use
:func:`get_public_key`, :func:`get_private_key_path`, and :func:`ensure_keypair`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from kurukuru.config import Settings, get_settings

logger = logging.getLogger("kurukuru.ssh")

# Base filename for the keypair; ``.pub`` is appended for the public half.
_KEY_BASENAME = "id_ed25519"
_KEY_COMMENT = "kurukuru-orchestrator"
_KEYGEN_TIMEOUT_SECONDS = 30


class SSHKeyError(Exception):
    """Raised when the orchestrator keypair cannot be created or read."""


@dataclass(frozen=True)
class KeyPaths:
    """Resolved absolute paths to the orchestrator keypair."""

    private: Path
    public: Path


def _key_paths(settings: Settings) -> KeyPaths:
    base = Path(settings.ssh_key_dir).expanduser()
    return KeyPaths(private=base / _KEY_BASENAME, public=base / f"{_KEY_BASENAME}.pub")


#: Mode OpenSSH demands of a private key. Anything more permissive and it
#: refuses to use the key at all ("UNPROTECTED PRIVATE KEY FILE"), which on
#: Linux turns every `ssh -i` into a failure with no obvious cause.
_KEY_MODE = 0o600
#: The key directory holds nothing but the keypair, so nobody else needs to
#: read it — and a directory others can list is how a key ends up copied.
_KEY_DIR_MODE = 0o700


def harden_private_key(private: Path) -> None:
    """Lock one private key to 0600 and its directory to 0700, on POSIX.

    Public because the orchestrator is no longer the only keypair on disk:
    ``POST /keypairs/generate`` writes others, and a key OpenSSH refuses to load
    is just as broken whoever created it.
    """
    if sys.platform == "win32":
        # NOTE: Windows ACL hardening (icacls) is deferred to a future phase.
        # os.chmod here is a no-op for real access control on NTFS.
        return
    try:
        private.parent.chmod(_KEY_DIR_MODE)
        private.chmod(_KEY_MODE)
    except OSError as exc:  # pragma: no cover - platform dependent
        logger.warning("Could not tighten permissions on %s: %s", private, exc)


def _harden_permissions(paths: KeyPaths) -> None:
    """Lock the orchestrator's private key down, every time it is handed out.

    This runs on **reuse as well as generation**, and that is the point.
    ``ssh-keygen`` creates a key 0600 already, so hardening only at generation
    time protects the one case that was never at risk. The cases that break are
    the ones where the key arrives some other way — restored from a backup,
    unpacked from a tarball, copied between machines, or written under a
    permissive umask — and on Linux OpenSSH *refuses* a group- or
    world-readable private key outright. Re-asserting the mode on every call is
    a stat and a chmod; discovering the alternative costs an afternoon.

    On Windows the mode bits are not real access control (that needs ACLs, and
    ``os.chmod`` only moves the read-only flag), so the call is skipped rather
    than pretended. Win32 OpenSSH does its own ACL check and is satisfied by a
    key the current user owns, which is what generation produces.
    """
    harden_private_key(paths.private)


def ensure_keypair(settings: Settings | None = None) -> KeyPaths:
    """Return the keypair paths, generating the keypair if it doesn't exist.

    Idempotent: an existing keypair is reused and never overwritten.
    """
    settings = settings or get_settings()
    paths = _key_paths(settings)

    if paths.private.exists() and paths.public.exists():
        logger.debug("Reusing existing orchestrator keypair at %s", paths.private)
        # Re-asserted on the reuse path too — see _harden_permissions for why
        # the generation-time chmod is the one that never mattered.
        _harden_permissions(paths)
        return paths

    paths.private.parent.mkdir(parents=True, exist_ok=True)

    # If only one half exists the pair is inconsistent; ssh-keygen refuses to
    # overwrite an existing private key, so surface a clear error instead.
    if paths.private.exists() != paths.public.exists():
        raise SSHKeyError(
            f"Inconsistent keypair: exactly one of {paths.private.name} / "
            f"{paths.public.name} exists in {paths.private.parent}. "
            "Remove the stray file and retry."
        )

    cmd = [
        settings.ssh_keygen_binary,
        "-t", "ed25519",
        "-N", "",                 # empty passphrase (non-interactive)
        "-f", str(paths.private),
        "-C", _KEY_COMMENT,
    ]
    logger.info("Generating orchestrator ed25519 keypair at %s", paths.private)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_KEYGEN_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SSHKeyError(
            f"ssh-keygen binary '{settings.ssh_keygen_binary}' not found — "
            "is OpenSSH installed and on PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SSHKeyError(
            f"ssh-keygen timed out after {_KEYGEN_TIMEOUT_SECONDS}s"
        ) from exc

    if proc.returncode != 0:
        raise SSHKeyError(
            f"ssh-keygen failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )

    if not (paths.private.exists() and paths.public.exists()):
        raise SSHKeyError("ssh-keygen reported success but keypair files are missing")

    _harden_permissions(paths)
    return paths


def get_public_key(settings: Settings | None = None) -> str:
    """Return the orchestrator's public key (generating the keypair if needed)."""
    settings = settings or get_settings()
    paths = ensure_keypair(settings)
    try:
        return paths.public.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SSHKeyError(f"Could not read public key {paths.public}: {exc}") from exc


def get_private_key_path(settings: Settings | None = None) -> Path:
    """Return the private key path (used with ``ssh -i``). Ensures it exists."""
    return ensure_keypair(settings).private
