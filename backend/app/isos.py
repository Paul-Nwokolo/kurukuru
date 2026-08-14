"""
Boot ISO catalog.

Files are placed in ``settings.iso_dir`` by hand — there is no upload endpoint,
which is the right call for a local-first tool: installer images run to
gigabytes, and pushing them through a browser buys nothing when the backend and
the user share a filesystem.

The only real work here is refusing to escape that directory. A filename arrives
from an HTTP request and ends up as a QEMU ``-drive file=`` argument, so
``../../../../etc/shadow`` would otherwise be attachable as a CD-ROM and readable
from inside a guest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings, get_settings

logger = logging.getLogger("iaas.isos")

_SUFFIX = ".iso"


class IsoError(Exception):
    """The requested ISO is missing or outside the ISO directory."""


@dataclass(frozen=True)
class IsoFile:
    """One ISO available to boot from."""

    name: str
    size_bytes: int
    modified_at: datetime


def iso_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.iso_dir).expanduser()


def list_isos(settings: Settings | None = None) -> list[IsoFile]:
    """Every ``.iso`` in the ISO directory, newest first. Missing dir = empty."""
    directory = iso_dir(settings)
    if not directory.is_dir():
        return []

    found: list[IsoFile] = []
    for child in directory.iterdir():
        if not child.is_file() or child.suffix.lower() != _SUFFIX:
            continue
        try:
            stat = child.stat()
        except OSError as exc:  # pragma: no cover - racing a deletion
            logger.warning("Could not stat %s: %s", child, exc)
            continue
        found.append(
            IsoFile(
                name=child.name,
                size_bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
        )
    return sorted(found, key=lambda i: i.modified_at, reverse=True)


def resolve_iso(name: str, settings: Settings | None = None) -> Path:
    """Resolve an ISO filename to an absolute path inside the ISO directory.

    Rejects anything that escapes, by comparing *resolved* paths rather than
    inspecting the string: ``..`` segments, absolute paths, and symlinks that
    point outside all collapse to a path that fails the containment check, which
    string matching alone would miss.
    """
    directory = iso_dir(settings).resolve()
    candidate = (directory / name).resolve()

    if candidate.parent != directory:
        raise IsoError(f"ISO '{name}' is outside the ISO directory")
    if candidate.suffix.lower() != _SUFFIX:
        raise IsoError(f"'{name}' is not an .iso file")
    if not candidate.is_file():
        raise IsoError(f"ISO '{name}' not found in {directory}")
    return candidate
