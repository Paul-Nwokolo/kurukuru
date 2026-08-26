"""
Carrying an existing install across the Phase 16 rename.

``~/.local-iaas`` holds instances, VM disks, the orchestrator keypair, imported
images, boot ISOs, volumes, database backups and the database itself. Phase 16
renamed the product, and with it that directory. **Every one of those things is
somebody's work**, so this is a data migration that happens to involve strings,
not a find-and-replace with a directory move stapled to it.

What makes it more than a rename is that the tree is not self-contained. Three
kinds of absolute path point *into* it and would be left dangling:

* ``qemu/instances/<name>/runtime.json`` — the boot ISO and each attached
  volume, replayed verbatim on every ``start``;
* database rows — ``keypairs.private_key_path`` and ``volumes.path``;
* **qcow2 headers** — an instance disk is a copy-on-write overlay whose backing
  file is recorded *inside the image*, as the absolute path it was created
  with. Nothing in the database or the runtime file mentions it. Miss this and
  the directory move succeeds, the dashboard looks healthy, and every VM built
  on a shared base image fails to start with "Could not open backing file".

The order below is chosen so that an interruption is survivable at every point.
Nothing is deleted, and the move is a **rename** — atomic, so there is no
half-moved tree to recover from. The one case that cannot be a rename is a state
directory on another volume; see ``_move_across_devices_or_refuse`` for why that
is the only case allowed to fall back to copying.

**Running VMs block the whole thing.** Windows refuses to rename a directory
containing an open file, and a running QEMU holds its disk open — so a naive
attempt fails part-way through the *checks* rather than part-way through the
tree, but the message would be a bare ``PermissionError`` naming a path. Phase
13 measured this. We detect it first and say what to do about it.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from kurukuru.engines.ports import is_port_free
from kurukuru.engines.process import pid_alive
from kurukuru.product import (
    CLI_NAME,
    DATABASE_LEAF,
    LEGACY_DATABASE_LEAF,
    LEGACY_STATE_DIR,
)

logger = logging.getLogger("kurukuru.migration")

#: Written into the new state directory once the move has completed, recording
#: what came from where. Idempotency does not depend on it — the legacy
#: directory no longer existing is the real guard — but a migration that leaves
#: no evidence of itself is one nobody can debug six months later.
MARKER_NAME = ".migrated-from"

#: SQLite's write-ahead sidecars. They belong to the ``.db`` file and must
#: travel with it under the new name: a WAL left behind is unreplayed committed
#: transactions, and a WAL beside the *wrong* database is a corrupt pair.
_SQLITE_SIDECARS = ("-wal", "-shm")

#: Columns holding an absolute path into the state directory. Kept as data for
#: the same reason ``Settings._ROOTED_DIRS`` is: a column added later that also
#: stores a path has one obvious place to be declared, and forgetting it is a
#: silently broken row rather than an import error.
_PATH_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("keypairs", "private_key_path", "id"),
    ("volumes", "path", "id"),
)

#: Fields of ``runtime.json`` holding an absolute path. ``volumes`` is a list,
#: and its **order is load-bearing** — QEMU enumerates virtio-blk devices in
#: argument order, so reordering it renames the guest's disks. Rewriting is
#: strictly element-wise for that reason.
_RUNTIME_PATH_FIELD = "iso_path"
_RUNTIME_PATH_LIST_FIELD = "volumes"


class StateMigrationBlocked(RuntimeError):
    """The migration cannot safely proceed, and starting anyway would lie.

    Raised rather than logged. If the backend came up regardless it would open
    a brand-new empty database at the new location while the user's real one sat
    untouched at the old one — an install that presents as having lost
    everything, which is the exact failure decision 26 exists to prevent.
    """


@dataclass(frozen=True)
class MigrationReport:
    """What actually happened, for the log and for the tests."""

    moved_from: Path
    moved_to: Path
    backup: Path | None = None
    database_renamed: bool = False
    runtime_files_rewritten: int = 0
    runtime_paths_rewritten: int = 0
    database_rows_rewritten: dict[str, int] = field(default_factory=dict)
    disks_rebased: tuple[str, ...] = ()
    disks_needing_attention: tuple[str, ...] = ()

    def describe(self) -> str:
        rows = ", ".join(
            f"{table}.{column}: {count}"
            for (table, column), count in sorted(self.database_rows_rewritten.items())
        )
        return (
            f"{self.moved_from} -> {self.moved_to}; "
            f"runtime files rewritten: {self.runtime_files_rewritten} "
            f"({self.runtime_paths_rewritten} paths); "
            f"database rows rewritten: {rows or 'none'}; "
            f"disks rebased: {len(self.disks_rebased)}"
        )


# --------------------------------------------------------------------------- #
# Path rewriting
# --------------------------------------------------------------------------- #
def _under(value: str, root: Path) -> bool:
    """Whether ``value`` is an absolute path inside ``root``.

    Compared by **path component**, not by string prefix. ``~/.local-iaas`` and
    ``~/.local-iaas-backup`` share a prefix and are different directories;
    rewriting the second because it starts with the first would silently
    repoint a path the user deliberately kept outside the tree.

    ``normcase`` is what makes this correct on Windows, where the comparison is
    case-insensitive *and* ``/`` and ``\\`` are the same separator. On POSIX it
    is the identity function, which is also correct there.
    """
    try:
        candidate = os.path.normcase(os.path.normpath(value))
        base = os.path.normcase(os.path.normpath(str(root)))
    except (TypeError, ValueError):
        return False
    return candidate == base or candidate.startswith(base + os.sep)


def _repoint(value: str, old_root: Path, new_root: Path) -> str:
    """``value``, moved from under ``old_root`` to under ``new_root``.

    Returns it unchanged when it does not live under ``old_root`` — an ISO the
    user keeps on another drive is not ours to move, and neither is the path
    recorded to it.
    """
    if not _under(value, old_root):
        return value
    relative = os.path.relpath(os.path.normpath(value), os.path.normpath(str(old_root)))
    return str(new_root) if relative == "." else str(new_root / relative)


# --------------------------------------------------------------------------- #
# The running-VM check
# --------------------------------------------------------------------------- #
def _runtime_files(state_dir: Path) -> list[Path]:
    """Every ``runtime.json`` under a state directory, in name order."""
    instances = state_dir / "qemu" / "instances"
    if not instances.is_dir():
        return []
    return sorted(
        path
        for child in instances.iterdir()
        if child.is_dir()
        for path in [child / "runtime.json"]
        if path.is_file()
    )


def _load_runtime(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable runtime file %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def running_instances(state_dir: Path) -> list[tuple[str, int]]:
    """``(name, pid)`` for every instance that still has a live QEMU behind it.

    Two signals, because either alone is wrong in a way that matters here:

    * a **live pid** alone can be a recycled process number, and refusing to
      migrate because some unrelated program inherited an old pid would be an
      upgrade the user cannot complete and cannot diagnose;
    * a **bound QMP port** alone cannot tell a live VM from something else that
      happened to take the port.

    Together they are the same tie-breaker ``QemuEngine._liveness`` already
    uses, and for the same reason: QEMU holds its QMP port for as long as it
    lives, so a dead QEMU has released it while a merely *busy* monitor has not.
    """
    running: list[tuple[str, int]] = []
    for path in _runtime_files(state_dir):
        data = _load_runtime(path)
        if data is None:
            continue
        pid = data.get("pid")
        qmp_port = data.get("qmp_port")
        if not isinstance(pid, int) or not pid_alive(pid):
            continue
        if isinstance(qmp_port, int) and is_port_free(qmp_port):
            # The pid is alive but nothing holds the monitor: QEMU is gone and
            # this number now belongs to someone else.
            logger.info(
                "Instance '%s' records pid %s, which is alive but has released "
                "QMP port %s — treating it as a recycled pid, not a running VM.",
                path.parent.name, pid, qmp_port,
            )
            continue
        running.append((path.parent.name, pid))
    return running


def _blocked_message(state_dir: Path, running: list[tuple[str, int]]) -> str:
    listed = "\n".join(f"  - {name} (pid {pid})" for name, pid in running)
    return (
        f"Cannot move {state_dir} to the new location: "
        f"{len(running)} instance(s) are still running and hold their disks "
        f"open. Windows will not rename a directory containing an open file, "
        f"and a half-moved state tree is not something to risk.\n\n"
        f"{listed}\n\n"
        f"Stop them, then start again. Either end those processes directly, or "
        f"run this release against the old directory for long enough to stop "
        f"them cleanly:\n\n"
        f"  KURUKURU_STATE_DIR={state_dir}  {CLI_NAME} stop <name>\n\n"
        f"Nothing has been moved or changed."
    )


# --------------------------------------------------------------------------- #
# The pieces of the move
# --------------------------------------------------------------------------- #
def _rename_database(state_dir: Path) -> bool:
    """``iaas.db`` -> ``kurukuru.db``, sidecars and all. Returns whether it ran.

    Checkpointed first so the write-ahead log's contents are folded into the
    ``.db`` itself, then all three files are renamed with the ``.db`` **last** —
    the discipline ``relocate_legacy_database`` already follows, so that an
    interruption leaves a database with its own WAL rather than one separated
    from it.
    """
    legacy = state_dir / LEGACY_DATABASE_LEAF
    target = state_dir / DATABASE_LEAF
    if not legacy.is_file():
        return False
    if target.exists():
        logger.warning(
            "Both %s and %s exist. Leaving both alone — which history to keep "
            "is a question for a human. The backend is using %s.",
            legacy, target, target,
        )
        return False

    try:
        connection = sqlite3.connect(str(legacy))
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        # Not fatal: the sidecars travel with the file either way, so SQLite
        # will replay the WAL on the next open. This only makes that unnecessary.
        logger.warning("Could not checkpoint %s before renaming it: %s", legacy, exc)

    for suffix in _SQLITE_SIDECARS:
        sidecar = Path(f"{legacy}{suffix}")
        if sidecar.exists():
            sidecar.rename(f"{target}{suffix}")
    legacy.rename(target)
    logger.info("Renamed the database: %s -> %s", legacy.name, target.name)
    return True


def _rewrite_runtime_files(state_dir: Path, old_root: Path, new_root: Path) -> tuple[int, int]:
    """Repoint ``iso_path`` and ``volumes`` in every runtime file.

    Returns ``(files changed, paths changed)``. Files are rewritten only when
    something actually changed, so an ISO living outside the state tree leaves
    its runtime file byte-identical.
    """
    files_changed = paths_changed = 0
    for path in _runtime_files(state_dir):
        data = _load_runtime(path)
        if data is None:
            continue

        changed = 0
        iso = data.get(_RUNTIME_PATH_FIELD)
        if isinstance(iso, str):
            moved = _repoint(iso, old_root, new_root)
            if moved != iso:
                data[_RUNTIME_PATH_FIELD] = moved
                changed += 1

        volumes = data.get(_RUNTIME_PATH_LIST_FIELD)
        if isinstance(volumes, list):
            # Element-wise and in place: the order is what the guest's /dev/vdb,
            # /dev/vdc ... follow, and an /etc/fstab was written against it.
            for index, volume in enumerate(volumes):
                if not isinstance(volume, str):
                    continue
                moved = _repoint(volume, old_root, new_root)
                if moved != volume:
                    volumes[index] = moved
                    changed += 1

        if not changed:
            continue
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error(
                "Could not rewrite %s: %s. Instance '%s' will not start until "
                "its iso_path/volumes point under %s.",
                path, exc, path.parent.name, new_root,
            )
            continue
        files_changed += 1
        paths_changed += changed
        logger.info("Repointed %d path(s) in %s", changed, path)
    return files_changed, paths_changed


def _rewrite_database_paths(
    database: Path, old_root: Path, new_root: Path
) -> dict[tuple[str, str], int]:
    """Repoint every absolute path stored in a table column.

    Done with :mod:`sqlite3` directly rather than through the ORM because this
    runs *before* anything opens the application's engine — the whole point is
    that the database is correct by the time the backend reads it.
    """
    counts: dict[tuple[str, str], int] = {}
    if not database.is_file():
        return counts

    connection = sqlite3.connect(str(database))
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table, column, key in _PATH_COLUMNS:
            if table not in tables:
                continue
            updates = []
            for identifier, value in connection.execute(
                f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL"
            ):
                if not isinstance(value, str):
                    continue
                moved = _repoint(value, old_root, new_root)
                if moved != value:
                    updates.append((moved, identifier))
            if not updates:
                continue
            connection.executemany(
                f"UPDATE {table} SET {column} = ? WHERE {key} = ?", updates
            )
            counts[(table, column)] = len(updates)
            logger.info("Repointed %s.%s on %d row(s)", table, column, len(updates))
        connection.commit()
    finally:
        connection.close()
    return counts


# --------------------------------------------------------------------------- #
# qcow2 backing files
# --------------------------------------------------------------------------- #
def _qcow2_files(state_dir: Path) -> list[Path]:
    """Every image this install owns that could have a backing file."""
    qemu = state_dir / "qemu"
    found: list[Path] = []
    for sub in ("instances", "volumes", "base-images"):
        root = qemu / sub
        if root.is_dir():
            found.extend(sorted(root.rglob("*.qcow2")))
    return found


def _backing_file(qemu_img: str, image: Path, timeout: int) -> str | None:
    """The backing path recorded *inside* ``image``, or None.

    ``qemu-img info`` reports both ``backing-file`` (as written into the header)
    and ``full-backing-filename`` (resolved). The header value is the one being
    corrected, so it is the one read.
    """
    result = subprocess.run(
        [qemu_img, "info", "--output=json", str(image)],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"qemu-img info failed on {image}")
    payload = json.loads(result.stdout)
    backing = payload.get("backing-filename") or payload.get("backing-file")
    return backing if isinstance(backing, str) and backing else None


def _rebase_backing_files(
    state_dir: Path, old_root: Path, new_root: Path, qemu_img: str, timeout: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Repoint every qcow2 whose backing file moved with the tree.

    ``qemu-img rebase -u`` is an **unsafe** rebase, and that is exactly right
    here: the safe form reads both images and rewrites the differences, which is
    what you want when the backing *content* changes. Nothing about the content
    changed — the same bytes are at a new path — so the only correct operation
    is to write the new path into the header and touch nothing else. The safe
    form would be hours of I/O to produce an identical file.

    Returns ``(rebased, needing attention)``. A failure here is reported rather
    than raised: the tree has already moved, and refusing to finish would leave
    the install in a state with no way forward. The remediation is one command
    per disk and it is logged verbatim.
    """
    rebased: list[str] = []
    stranded: list[str] = []
    for image in _qcow2_files(state_dir):
        try:
            backing = _backing_file(qemu_img, image, timeout)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.error(
                "Could not read the backing file of %s: %s. If this disk is an "
                "overlay, check it with: %s info %s",
                image, exc, qemu_img, image,
            )
            stranded.append(str(image))
            continue

        if backing is None:
            continue  # a base image, a blank ISO-install disk, or a volume
        moved = _repoint(backing, old_root, new_root)
        if moved == backing:
            continue  # backed by something outside the tree; not ours to move

        command = [
            qemu_img, "rebase", "-u", "-f", "qcow2", "-F", "qcow2",
            "-b", moved, str(image),
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout, check=False
            )
            if result.returncode != 0:
                raise OSError(result.stderr.strip() or "qemu-img rebase failed")
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error(
                "Could not repoint the backing file of %s: %s. That VM will not "
                "start until this is run by hand:\n  %s",
                image, exc, subprocess.list2cmdline(command),
            )
            stranded.append(str(image))
            continue
        rebased.append(str(image))
        logger.info("Repointed the backing file of %s: %s -> %s", image, backing, moved)
    return tuple(rebased), tuple(stranded)


# --------------------------------------------------------------------------- #
# The migration itself
# --------------------------------------------------------------------------- #
def _move_across_devices_or_refuse(legacy: Path, target: Path, failure: OSError) -> None:
    """Handle a failed rename. Falls back to copying **only** across devices.

    The distinction is not pedantry, it is the difference between a safe
    migration and a destroyed one. ``rename`` is atomic: it happens or it does
    not, and there is no half-moved tree either way. ``shutil.move`` across
    devices is *copy, then delete* — twenty gigabytes of it — so reaching for it
    whenever a rename fails would turn "a file in here is open" into a long
    partial copy of the user's entire install.

    And "a file in here is open" is the *likely* failure, not an exotic one.
    Windows refuses to rename a directory containing an open file, so a running
    VM or a still-running older backend produces exactly this. Those get a
    refusal that names the cause, because there is nothing to fall back to:
    copying around an open disk image would produce a torn one.

    A genuine cross-device move — a state directory on another volume, reached
    through a junction — has no atomic form on any platform, so it copies. If
    that copy fails part-way the partial target is removed, because leaving it
    would trip the "never write onto a target that has contents" guard on the
    next start and lock the user out of retrying.
    """
    cross_device = failure.errno == errno.EXDEV or getattr(failure, "winerror", None) == 17
    if not cross_device:
        raise StateMigrationBlocked(
            f"Could not move {legacy} to {target}: {failure}\n\n"
            f"On Windows this almost always means a file in that directory is "
            f"open. Look for a virtual machine that is still running, an older "
            f"copy of the backend still serving, or a shell or editor sitting "
            f"inside the directory.\n\n"
            f"Nothing has been moved or changed. Close whatever holds it and "
            f"start again, or set KURUKURU_STATE_DIR={legacy} to keep using it "
            f"where it is."
        ) from failure

    logger.info(
        "%s and %s are on different volumes, so this is a copy rather than a "
        "rename. It moves everything in the directory and can take a while.",
        legacy, target,
    )
    try:
        shutil.move(str(legacy), str(target))
    except OSError as move_exc:
        if target.exists():
            logger.warning("Removing the incomplete copy at %s", target)
            shutil.rmtree(target, ignore_errors=True)
        raise StateMigrationBlocked(
            f"Could not copy {legacy} to {target}: {move_exc}\n\n"
            f"The original is untouched and any partial copy has been removed. "
            f"Free some space or move the directory by hand, or set "
            f"KURUKURU_STATE_DIR={legacy} to keep using it where it is."
        ) from move_exc


def _is_empty(directory: Path) -> bool:
    """Whether a directory holds nothing but this module's own marker."""
    try:
        return all(child.name == MARKER_NAME for child in directory.iterdir())
    except OSError:
        return False


def migrate_state_dir(
    settings, engine_in_use=None, *, legacy_dir: str = LEGACY_STATE_DIR
) -> MigrationReport | None:
    """Move a pre-Phase-16 state directory to where the backend now looks.

    Returns the report, or None when there was nothing to do — which is the
    normal case on every start after the first, and on every fresh install.

    Deliberately narrow, in the same shape as ``relocate_legacy_database``:

    * **Only on the default layout.** If ``state_dir`` was set explicitly, that
      is a decision, and hauling twenty gigabytes of VM disks to a location the
      operator chose *for something else* is not a migration's call to make. The
      old directory is reported and left alone.
    * **Only when the engine about to be used is the one pointing there.** Belt
      to those braces, and it is what keeps a tree-moving migration away from
      the test suite: every test runs against an engine of its own, so neither
      condition holds and the scan never starts. A guard with one condition
      would be one careless fixture away from moving a developer's real install
      into a ``tmp_path``.
    * **Never onto data.** A target that already has anything in it wins; two
      state trees is a question for a human.
    * **Running VMs refuse the whole operation**, before anything is touched.
    * **A database backup is taken first**, using the machinery Phase 14 built
      for exactly this.

    :raises StateMigrationBlocked: when instances are running. The backend must
        not start: it would open an empty database at the new path and present
        as an install that has lost everything.
    """
    from kurukuru.database import _engine_file, backup_database_file  # circular at module scope

    target = Path(settings.state_dir).expanduser()
    legacy = Path(legacy_dir).expanduser()

    if legacy == target:
        return None  # already there, or pointed at the old directory on purpose
    if not legacy.is_dir():
        return None
    if engine_in_use is not None and _engine_file(engine_in_use) != settings.database_path:
        return None  # an engine of the suite's own, or a server URL
    if str(settings.state_dir) != str(type(settings).model_fields["state_dir"].default):
        logger.warning(
            "%s still exists, but state_dir is set to %s. Not moving it — an "
            "explicitly configured location is a decision. Move it by hand, or "
            "unset the override to have it migrated automatically.",
            legacy, target,
        )
        return None
    if target.exists() and not _is_empty(target):
        logger.warning(
            "Both %s and %s exist and have contents. Leaving both alone; the "
            "backend is using %s. Merge them by hand once you have decided "
            "which install you want.",
            legacy, target, target,
        )
        return None

    blocking = running_instances(legacy)
    if blocking:
        raise StateMigrationBlocked(_blocked_message(legacy, blocking))

    logger.info("Migrating the state directory: %s -> %s", legacy, target)

    # Before the move, into the tree that is about to travel — so the backup
    # lands in the new location with everything else rather than being left
    # behind at a path that is about to stop existing.
    backup = backup_database_file(
        legacy / LEGACY_DATABASE_LEAF,
        legacy / "backups",
        retention=getattr(settings, "db_backup_retention", 5),
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # Only the marker was in it; the rename needs the name free.
        shutil.rmtree(target)
    try:
        legacy.rename(target)
    except OSError as exc:
        _move_across_devices_or_refuse(legacy, target, exc)

    database_renamed = _rename_database(target)
    files, paths = _rewrite_runtime_files(target, legacy, target)
    rows = _rewrite_database_paths(target / DATABASE_LEAF, legacy, target)
    rebased, stranded = _rebase_backing_files(
        target, legacy, target,
        qemu_img=getattr(settings, "qemu_img_binary", "qemu-img"),
        timeout=getattr(settings, "cli_timeout_seconds", 30),
    )

    report = MigrationReport(
        moved_from=legacy,
        moved_to=target,
        backup=backup,
        database_renamed=database_renamed,
        runtime_files_rewritten=files,
        runtime_paths_rewritten=paths,
        database_rows_rewritten=rows,
        disks_rebased=rebased,
        disks_needing_attention=stranded,
    )
    _write_marker(target, report)
    logger.info("State directory migrated. %s", report.describe())
    if stranded:
        logger.error(
            "%d image(s) still reference the old location and need the "
            "qemu-img command logged above: %s",
            len(stranded), ", ".join(stranded),
        )
    return report


def _write_marker(target: Path, report: MigrationReport) -> None:
    """Record what happened, next to what it happened to."""
    payload = {
        "migrated_at": datetime.now().astimezone().isoformat(),
        "from": str(report.moved_from),
        "to": str(report.moved_to),
        "database_backup": str(report.backup) if report.backup else None,
        "database_renamed": report.database_renamed,
        "runtime_files_rewritten": report.runtime_files_rewritten,
        "runtime_paths_rewritten": report.runtime_paths_rewritten,
        "database_rows_rewritten": {
            f"{table}.{column}": count
            for (table, column), count in report.database_rows_rewritten.items()
        },
        "disks_rebased": list(report.disks_rebased),
        "disks_needing_attention": list(report.disks_needing_attention),
    }
    try:
        (target / MARKER_NAME).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        # The migration succeeded; only the receipt failed. Saying so is better
        # than failing a completed move over a note.
        logger.warning("Could not write %s: %s", target / MARKER_NAME, exc)
