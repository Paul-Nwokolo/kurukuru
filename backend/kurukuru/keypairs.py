"""
OpenSSH public-key parsing, fingerprinting and generation.

Everything that knows what an SSH key *is* lives here; the router and the
models deal in already-validated values.

Parsing is done in-process rather than by shelling out to ``ssh-keygen -lf``.
The brief allows either, and the subprocess would mean writing a caller-supplied
string to a temporary file and running a binary over it, on a request path, to
learn something a dozen lines of ``base64`` and ``hashlib`` already know. The
wire format is fixed and self-describing, so the structural check below is also
a stronger validation than "ssh-keygen exited 0": it proves the blob's embedded
type matches the declared one, which is what makes a pasted key trustworthy
enough to bake into a guest.

Generation still uses ``ssh-keygen`` — writing private key files is exactly the
job of the tool that already owns the orchestrator's own keypair.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from kurukuru.config import Settings, get_settings

#: Key types we accept. These are the algorithms OpenSSH itself still offers by
#: default; anything else is refused with its name rather than silently stored,
#: because a key we cannot fingerprint is a key we cannot show the user.
SUPPORTED_KEY_TYPES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
)

#: Human label per wire type, for the UI's "Type" column.
_TYPE_LABELS = {
    "ssh-ed25519": "ed25519",
    "ssh-rsa": "rsa",
    "ecdsa-sha2-nistp256": "ecdsa-256",
    "ecdsa-sha2-nistp384": "ecdsa-384",
    "ecdsa-sha2-nistp521": "ecdsa-521",
}

_KEYGEN_TIMEOUT_SECONDS = 30
_SLUG = re.compile(r"[^a-z0-9-]+")


class KeyPairError(ValueError):
    """A public key could not be parsed, or a keypair could not be generated."""


@dataclass(frozen=True)
class ParsedPublicKey:
    """A validated OpenSSH public key, normalised."""

    #: Wire type, e.g. ``ssh-ed25519``.
    key_type: str
    #: Short label for display, e.g. ``ed25519``.
    label: str
    #: ``SHA256:...`` in OpenSSH's spelling.
    fingerprint: str
    #: The key as it should be stored: type, blob, and comment if there was one.
    normalised: str
    comment: str | None


def parse_public_key(text: str) -> ParsedPublicKey:
    """Validate an OpenSSH public key and describe it.

    Raises :class:`KeyPairError` with a message naming the actual problem —
    these strings are pasted by hand, so "invalid key" would leave the user with
    nowhere to go.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise KeyPairError("No key was supplied.")
    if cleaned.startswith("-----BEGIN"):
        raise KeyPairError(
            "That looks like a *private* key. Paste the public half instead — "
            "the file ending in .pub, e.g. ~/.ssh/id_ed25519.pub."
        )

    parts = cleaned.split(" ")
    if len(parts) < 2:
        raise KeyPairError(
            "A public key needs at least a type and a body, e.g. "
            "'ssh-ed25519 AAAAC3Nza...'."
        )

    key_type, blob_b64 = parts[0], parts[1]
    comment = " ".join(parts[2:]) or None

    if key_type not in SUPPORTED_KEY_TYPES:
        raise KeyPairError(
            f"Unsupported key type '{key_type}'. Supported: "
            f"{', '.join(SUPPORTED_KEY_TYPES)}."
        )

    try:
        blob = base64.b64decode(blob_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyPairError(f"The key body is not valid base64: {exc}") from exc

    # Every OpenSSH blob begins with its own type as a length-prefixed string.
    # Checking it catches a truncated paste and a body that belongs to a
    # different algorithm than the one declared — neither of which base64
    # decoding alone would notice.
    embedded = _first_string(blob)
    if embedded is None:
        raise KeyPairError("The key body is truncated or not an OpenSSH key blob.")
    if embedded != key_type:
        raise KeyPairError(
            f"The key body declares '{embedded}' but the line says '{key_type}'."
        )

    digest = hashlib.sha256(blob).digest()
    fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")

    normalised = f"{key_type} {blob_b64}" + (f" {comment}" if comment else "")
    return ParsedPublicKey(
        key_type=key_type,
        label=_TYPE_LABELS.get(key_type, key_type),
        fingerprint=fingerprint,
        normalised=normalised,
        comment=comment,
    )


def _first_string(blob: bytes) -> str | None:
    """Read the leading length-prefixed string from an SSH wire blob."""
    if len(blob) < 4:
        return None
    (length,) = struct.unpack(">I", blob[:4])
    if length > len(blob) - 4 or length == 0:
        return None
    try:
        return blob[4 : 4 + length].decode("ascii")
    except UnicodeDecodeError:
        return None


def generate_keypair(name: str, settings: Settings | None = None) -> tuple[Path, str]:
    """Create an ed25519 keypair on disk; return (private path, public key).

    The filename is derived from the name but never *is* the name: a keypair
    name is free text and this is a filesystem path, so it is slugged and given
    a uuid suffix. That also means two keypairs called "laptop" cannot fight
    over one file.
    """
    settings = settings or get_settings()
    directory = Path(settings.ssh_key_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    slug = _SLUG.sub("-", name.strip().lower()).strip("-") or "keypair"
    private = directory / f"{slug[:32]}-{uuid.uuid4().hex[:8]}"

    cmd = [
        settings.ssh_keygen_binary,
        "-t", "ed25519",
        "-N", "",                      # no passphrase: nothing could supply one
        "-f", str(private),
        "-C", name,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_KEYGEN_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise KeyPairError(
            f"ssh-keygen binary '{settings.ssh_keygen_binary}' not found — is "
            "OpenSSH installed and on PATH?"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise KeyPairError(f"ssh-keygen failed: {exc}") from exc

    if proc.returncode != 0:
        raise KeyPairError(
            f"ssh-keygen failed (exit {proc.returncode}): {(proc.stderr or '').strip()}"
        )

    public_path = private.with_suffix(private.suffix + ".pub")
    if not (private.exists() and public_path.exists()):
        raise KeyPairError("ssh-keygen reported success but wrote no keypair")

    # Same hardening the orchestrator key gets, and for the same reason: on
    # POSIX, OpenSSH refuses a private key others can read.
    from kurukuru.ssh_keys import harden_private_key

    harden_private_key(private)
    return private, public_path.read_text(encoding="utf-8").strip()
