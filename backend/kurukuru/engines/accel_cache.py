"""Remembering, across restarts, that hardware acceleration works here.

``QemuEngine._probe_accel`` costs **6.0 seconds**, measured on this host, and
it is the bulk of an 8.4-second cold start. The cost is structural rather than
incidental: the probe starts QEMU paused with ``-S`` and uses the *timeout* as
its success signal, because an unusable accelerator makes QEMU exit at once.

Which produces the fact this module is built on:

    **A working accelerator is the slow answer. A broken one is instant.**

So only a positive result is worth storing. A negative costs nothing to
re-derive, and re-deriving it every start is what keeps a user who has just
enabled Windows Hypervisor Platform from having to find a cache file before
their machine gets faster.

**What the key has to contain.** The answer depends on the binary (a different
QEMU build may support a different accelerator), on its version, and on the
platform. All three are in the key, so an upgrade, a reinstall or a move to a
different QEMU re-probes rather than inheriting an answer about a binary that
is no longer there.

**What the key cannot contain**, and what the TTL is therefore for: whether
the *host* still offers the accelerator. Turning Windows Hypervisor Platform
off changes nothing about the binary, so a stored "whpx works" would outlive
it. Two things bound that: a failed launch clears the entry (see
``QemuEngine.provision_instance``), and an entry expires on its own after
:data:`_TTL_SECONDS` regardless. Neither is needed often; both mean the worst
case is one slow start, not a permanently wrong answer.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("kurukuru.qemu.accel_cache")

#: Cache file, under the engine's own directory with the rest of its state.
_FILENAME = "accel-probe.json"

#: How long a stored positive is trusted. Two weeks: long enough that the
#: probe is paid once and effectively never again, short enough that a host
#: whose accelerator was turned off corrects itself without anyone having to
#: know this file exists.
_TTL_SECONDS = 14 * 24 * 60 * 60

#: Bumped if the shape below ever changes, so an old file is ignored rather
#: than misread.
_SCHEMA = 1


def _cache_path(qemu_dir: Path) -> Path:
    return qemu_dir / _FILENAME


def _identity(binary: str, version: str | None) -> dict[str, object]:
    """What makes this a different question from the last one.

    Size and mtime alongside the path, because a rebuilt or replaced binary at
    the same path with the same reported version is still a different binary —
    and this project has already been bitten once by trusting a name over the
    file behind it.
    """
    identity: dict[str, object] = {
        "binary": binary,
        "version": version,
        "platform": sys.platform,
        "size": None,
        "mtime": None,
    }
    try:
        stat = Path(binary).stat()
    except OSError:
        # A bare name resolved through PATH, or a binary we cannot stat. The
        # remaining fields still key the entry; they are simply less specific.
        return identity
    identity["size"] = stat.st_size
    identity["mtime"] = int(stat.st_mtime)
    return identity


def load(
    qemu_dir: Path, binary: str, version: Callable[[], str | None]
) -> str | None:
    """A previously measured working accelerator, or None. Never raises.

    None means "probe": no entry, a stale one, one for a different binary, or
    a file this build cannot read. Every one of those is answered by doing the
    work, which is the safe direction for a cache to fail in.

    ``version`` is a *callable*, not a string, and that is worth the small
    awkwardness: reading it means running ``qemu-system-x86_64 --version``, and
    on the common cold path — no cache file at all — the answer is None before
    the version is needed. Passing the value eagerly spent a subprocess on
    every start that had nothing to load.
    """
    path = _cache_path(qemu_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Ignoring unreadable accelerator cache at %s: %s", path, exc)
        return None

    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
        return None

    if payload.get("identity") != _identity(binary, version()):
        logger.debug("Accelerator cache is for a different QEMU; re-probing")
        return None

    stored_at = payload.get("stored_at")
    if not isinstance(stored_at, (int, float)):
        return None
    age = time.time() - stored_at
    # Negative age means the clock moved backwards since the entry was
    # written. Treat it as stale rather than as infinitely fresh.
    if age < 0 or age > _TTL_SECONDS:
        logger.debug("Accelerator cache has expired; re-probing")
        return None

    accel = payload.get("accel")
    if not isinstance(accel, str) or not accel:
        return None

    logger.debug("Accelerator '%s' taken from %s", accel, path)
    return accel


def store(qemu_dir: Path, binary: str, version: str | None, accel: str) -> None:
    """Record a working accelerator. Never raises.

    A cache that cannot be written is a slow start, not a failure, so every
    error here is logged at debug and swallowed — this runs during startup on
    a path that must not acquire a new way to fail.

    Written through a temporary file and replaced, so a backend killed
    mid-write leaves the previous entry rather than a truncated one that the
    next start would have to recognise as damaged.
    """
    path = _cache_path(qemu_dir)
    payload = {
        "schema": _SCHEMA,
        "identity": _identity(binary, version),
        "accel": accel,
        "stored_at": time.time(),
        # For a human reading the file. Nothing parses it.
        "note": (
            "Kurukuru caches the result of its hardware-acceleration probe "
            "here because the probe takes about six seconds. Deleting this "
            "file is safe: it will be measured again."
        ),
    }
    temporary = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        logger.debug("Could not write the accelerator cache to %s: %s", path, exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def clear(qemu_dir: Path) -> None:
    """Forget the stored accelerator. Never raises.

    Called when a launch fails: the stored answer said this accelerator works
    and the evidence now says otherwise, so the next start measures again
    rather than repeating a launch that cannot succeed.
    """
    try:
        _cache_path(qemu_dir).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best effort
        logger.debug("Could not clear the accelerator cache: %s", exc)
