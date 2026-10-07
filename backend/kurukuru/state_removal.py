"""
Removing a Linux install's data: measured, asked for, and never deleted.

Matches what 0.1.3 made the Windows uninstaller do (DECISIONS #63): say what
is about to go, with a size against each kind of thing, and make the removal
undoable. Windows has the Recycle Bin; Linux has the freedesktop trash where a
desktop provides one, and nothing at all on a server. So, in order:

1. ``gio trash`` — GLib's implementation of the freedesktop trash spec, present
   on any GNOME-family desktop and on many servers through ``glib2``.
2. ``trash-put`` — trash-cli, the same spec without a desktop.
3. Neither: **rename the tree aside**, to ``<dir>.removed-<timestamp>`` beside
   it. A rename on one filesystem is atomic and copies nothing, so it costs no
   space and cannot half-fail — and the data is exactly where the user can
   see it, with the ``rm -rf`` printed for them to run themselves.

What this never does is delete. The standing rule (memory, CONTRIBUTING):
state directories are renamed aside, never removed, and never without asking —
backups live *inside* the state directory, so a delete takes the only safety
net with it, which is precisely what 0.1.2's uninstaller did.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TRASH_TIMEOUT_SECONDS = 600  # a trash across filesystems is a copy; VM disks are large

#: (label, path relative to the state directory). Measured in this order; what
#: is left over is reported as "other".
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("VM disks", "qemu/instances"),
    ("Images", "qemu/base-images"),
    ("Volumes", "qemu/volumes"),
    ("Snapshots", "qemu/snapshots"),
    ("Boot media (ISOs)", "isos"),
    ("Database backups", "backups"),
    ("SSH keys", "keys"),
)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def inventory(root: Path) -> list[tuple[str, int]]:
    """(label, bytes) for each kind of thing present, largest kinds first."""
    rows: list[tuple[str, int]] = []
    counted = 0
    for label, rel in _CATEGORIES:
        target = root / rel
        if target.exists():
            size = _size(target)
            counted += size
            if size:
                rows.append((label, size))
    db = sum(_size(p) for p in root.glob("kurukuru.db*"))
    if db:
        rows.append(("Database", db))
        counted += db
    other = _size(root) - counted
    if other > 0:
        rows.append(("Other", other))
    return rows


@dataclass(frozen=True)
class Removal:
    method: str          # "gio trash" | "trash-put" | "moved aside"
    source: Path
    destination: Path | None  # set when moved aside


def aside_path(root: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    candidate = root.with_name(f"{root.name}.removed-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = root.with_name(f"{root.name}.removed-{stamp}-{n}")
    return candidate


def remove(root: Path, *, which=shutil.which, runner=subprocess.run, now=None) -> Removal:
    """Trash ``root`` if there is a trash, otherwise rename it aside. Never deletes."""
    for tool, command in (("gio trash", ["gio", "trash", str(root)]),
                          ("trash-put", ["trash-put", str(root)])):
        if which(command[0]) is None:
            continue
        result = runner(command, capture_output=True, text=True,
                        timeout=TRASH_TIMEOUT_SECONDS, check=False)
        if result.returncode == 0 and not root.exists():
            return Removal(tool, root, None)
        # A trash that failed (a cross-device trash it refused, no trash dir on
        # this mount) falls through to the rename rather than to anything worse.
    destination = aside_path(root, now)
    root.rename(destination)
    return Removal("moved aside", root, destination)
