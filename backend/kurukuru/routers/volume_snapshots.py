"""
Volume snapshot lifecycle, nested under a volume.

Deliberately its own thing rather than an extension of instance snapshots, and
the distinction is worth stating plainly because both dialogs have to say it:

    An instance snapshot captures the instance's overlay and **not** its
    attached volumes. A volume snapshot captures the volume and **not** the
    instance it happens to be attached to.

Two independent operations on two independent files. Restoring one does not
restore the other, and a user who takes one and expects both has lost data in a
way nothing will announce.

**The safety rule is "no running instance holds this volume".** Not "detached".
The invariant that matters is the one DECISIONS #15 established for instance
snapshots: ``qemu-img`` writing to a qcow2 that a live QEMU has open is how the
file gets corrupted, and on Windows there is no lock to stop it. A volume
attached to a *stopped* instance is not open by anything, so snapshotting it is
exactly as safe as snapshotting a detached one — and since DECISIONS #22 already
requires a stopped instance to detach at all, demanding detachment first would
add a stop/attach cycle without adding any safety.

No events are recorded here. The event log is an *instance's* history by schema
(``InstanceEvent.instance_id``), and a detached volume has no instance to hang
one on. Inventing a null-instance event to make the feed look complete would
put a row in the table that every reader of that table has to special-case.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlmodel import Session, select

from kurukuru.config import Settings, get_settings
from kurukuru.database import engine as db_engine
from kurukuru.database import get_session
from kurukuru.engines import (
    ComputeEngine,
    ComputeEngineError,
    EngineRegistry,
    HypervisorUnavailableError,
    UnknownEngineError,
    get_engine_registry,
)
from kurukuru.host_capacity import invalidate_cache
from kurukuru.models import (
    Instance,
    InstanceStatus,
    SnapshotStatus,
    Volume,
    VolumeSnapshot,
    VolumeSnapshotCreate,
    VolumeSnapshotRead,
    VolumeStatus,
)
from kurukuru.routers.volumes import volume_path

logger = logging.getLogger("kurukuru.volume_snapshots")

router = APIRouter(prefix="/volumes/{volume_id}/snapshots", tags=["volume snapshots"])


# --------------------------------------------------------------------------- #
# Lookups and guards
# --------------------------------------------------------------------------- #
def _volume_or_404(session: Session, volume_id: str) -> Volume:
    volume = session.get(Volume, volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume '{volume_id}' not found")
    return volume


def _snapshot_or_404(session: Session, volume_id: str, snapshot_id: str) -> VolumeSnapshot:
    snapshot = session.get(VolumeSnapshot, snapshot_id)
    if snapshot is None or snapshot.volume_id != volume_id:
        raise HTTPException(
            status_code=404, detail=f"Volume snapshot '{snapshot_id}' not found"
        )
    return snapshot


def _engine_for(registry: EngineRegistry, volume: Volume) -> ComputeEngine:
    """The engine that owns this volume's file.

    A volume is not bound to an engine the way an instance is — it is a file.
    The default engine is the one that created it and the only one that has
    ever been able to, so it is the one asked to snapshot it.
    """
    try:
        compute = registry.get(None)
    except UnknownEngineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not getattr(compute, "supports_volume_snapshots", False):
        raise HTTPException(
            status_code=409,
            detail="This hypervisor engine does not support volume snapshots",
        )
    return compute


def _require_not_held_by_a_running_instance(
    session: Session, volume: Volume, action: str
) -> None:
    """Refuse while a live QEMU has the file open, and say why.

    Detached is fine. Attached to a stopped instance is fine — nothing has the
    file open. The check is on the *instance's* state rather than the volume's
    attachment because that is the thing that actually determines whether a
    process is holding it.
    """
    if not volume.attached_instance_id:
        return
    instance = session.get(Instance, volume.attached_instance_id)
    if instance is None or instance.status is InstanceStatus.STOPPED:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {action} '{volume.name}' while '{instance.name}' is "
            f"{instance.status.value}. A snapshot copies the disk at rest, and "
            f"reading this file while the instance is writing to it would "
            f"capture a filesystem mid-write. Stop the instance first — the "
            f"volume can stay attached."
        ),
    )


def _require_usable(volume: Volume, action: str) -> None:
    """Refuse a volume that is not finished being created, or is failed."""
    if volume.status in (VolumeStatus.AVAILABLE, VolumeStatus.ATTACHED):
        return
    raise HTTPException(
        status_code=409,
        detail=f"Cannot {action} '{volume.name}' while it is {volume.status.value}",
    )


def _file_for(volume: Volume, settings: Settings) -> str:
    return volume.path or str(volume_path(volume, settings))


def _fail(session: Session, snapshot: VolumeSnapshot, message: str) -> None:
    snapshot.status = SnapshotStatus.ERROR
    snapshot.error_message = message
    session.add(snapshot)
    session.commit()


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #
def _create_job(snapshot_id: str, volume_file: str) -> None:
    registry = get_engine_registry()
    with Session(db_engine) as session:
        snapshot = session.get(VolumeSnapshot, snapshot_id)
        if snapshot is None:  # deleted before the job ran
            return
        try:
            info = registry.get(None).create_volume_snapshot(
                volume_file, snapshot.name
            )
        except (ComputeEngineError, UnknownEngineError) as exc:
            logger.error("Volume snapshot '%s' failed: %s", snapshot.name, exc)
            _fail(session, snapshot, str(exc))
            return

        snapshot.status = SnapshotStatus.AVAILABLE
        snapshot.size_bytes = info.size_bytes
        snapshot.error_message = None
        session.add(snapshot)
        session.commit()
        # The snapshot's data lives inside the volume file, so free disk moved.
        invalidate_cache()
        logger.info("Volume snapshot '%s' available", snapshot.name)


def _delete_job(snapshot_id: str, volume_file: str) -> None:
    registry = get_engine_registry()
    with Session(db_engine) as session:
        snapshot = session.get(VolumeSnapshot, snapshot_id)
        if snapshot is None:
            return
        try:
            registry.get(None).delete_volume_snapshot(volume_file, snapshot.name)
        except (ComputeEngineError, UnknownEngineError) as exc:
            logger.error("Deleting volume snapshot '%s' failed: %s", snapshot.name, exc)
            _fail(session, snapshot, str(exc))
            return

        name = snapshot.name
        session.delete(snapshot)
        session.commit()
        invalidate_cache()
        logger.info("Deleted volume snapshot '%s'", name)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=VolumeSnapshotRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Snapshot a volume (async)",
)
def create_volume_snapshot(
    volume_id: str,
    payload: VolumeSnapshotCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
    settings: Settings = Depends(get_settings),
) -> VolumeSnapshot:
    """Capture the volume's disk. Returns 202; poll the row for progress.

    This does **not** capture any instance the volume is attached to.
    """
    volume = _volume_or_404(session, volume_id)
    _engine_for(registry, volume)
    _require_usable(volume, "snapshot")
    _require_not_held_by_a_running_instance(session, volume, "snapshot")

    existing = session.exec(
        select(VolumeSnapshot)
        .where(VolumeSnapshot.volume_id == volume_id)
        .where(VolumeSnapshot.name == payload.name)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"'{volume.name}' already has a snapshot named '{payload.name}'",
        )

    snapshot = VolumeSnapshot(
        volume_id=volume_id, name=payload.name, description=payload.description
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    background_tasks.add_task(_create_job, snapshot.id, _file_for(volume, settings))
    logger.info("Accepted snapshot '%s' of volume '%s'", snapshot.name, volume.name)
    return snapshot


@router.get(
    "", response_model=list[VolumeSnapshotRead], summary="List a volume's snapshots"
)
def list_volume_snapshots(
    volume_id: str,
    session: Session = Depends(get_session),
) -> list[VolumeSnapshot]:
    _volume_or_404(session, volume_id)
    return list(
        session.exec(
            select(VolumeSnapshot)
            .where(VolumeSnapshot.volume_id == volume_id)
            .order_by(VolumeSnapshot.created_at)
        ).all()
    )


@router.post(
    "/{snapshot_id}/restore",
    response_model=VolumeSnapshotRead,
    summary="Restore a volume to a snapshot",
)
def restore_volume_snapshot(
    volume_id: str,
    snapshot_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
    settings: Settings = Depends(get_settings),
) -> VolumeSnapshot:
    """Roll the volume back, discarding everything written since the snapshot.

    Synchronous for the same reason an instance restore is: applying a qcow2
    snapshot rewrites the L1 table rather than copying data.

    The running-instance guard applies here too, and matters more than it does
    for a snapshot: rewriting a disk underneath a live guest does not merely
    capture a bad copy, it replaces the filesystem the guest believes it has.
    """
    volume = _volume_or_404(session, volume_id)
    snapshot = _snapshot_or_404(session, volume_id, snapshot_id)
    _engine_for(registry, volume)
    _require_usable(volume, "restore")
    _require_not_held_by_a_running_instance(session, volume, "restore")

    if snapshot.status is not SnapshotStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=f"Snapshot '{snapshot.name}' is {snapshot.status.value}, not Available",
        )

    try:
        registry.get(None).restore_volume_snapshot(
            _file_for(volume, settings), snapshot.name
        )
    except HypervisorUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ComputeEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info("Restored volume '%s' to snapshot '%s'", volume.name, snapshot.name)
    return snapshot


@router.delete(
    "/{snapshot_id}",
    response_model=VolumeSnapshotRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Delete a volume snapshot (async)",
)
def delete_volume_snapshot(
    volume_id: str,
    snapshot_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
    settings: Settings = Depends(get_settings),
) -> VolumeSnapshot:
    """Remove a snapshot. Returns 202; the row disappears when it is gone."""
    volume = _volume_or_404(session, volume_id)
    snapshot = _snapshot_or_404(session, volume_id, snapshot_id)
    _engine_for(registry, volume)
    _require_not_held_by_a_running_instance(session, volume, "delete a snapshot of")

    snapshot.status = SnapshotStatus.DELETING
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    background_tasks.add_task(_delete_job, snapshot.id, _file_for(volume, settings))
    return snapshot


def delete_snapshots_for_volume(session: Session, volume_id: str) -> int:
    """Drop every snapshot row for a volume. Returns how many.

    Called when a volume is deleted. No hypervisor work is needed — the
    snapshots live *inside* the volume's qcow2, and that file is unlinked by the
    delete route. Leaving the rows behind would advertise restore points whose
    data was deleted seconds earlier.
    """
    rows = session.exec(
        select(VolumeSnapshot).where(VolumeSnapshot.volume_id == volume_id)
    ).all()
    for row in rows:
        session.delete(row)
    if rows:
        # Flushed here, not left to the caller's commit. There is no ORM
        # relationship declared between these tables, so SQLAlchemy does not
        # know the snapshots depend on the volume and is free to emit the
        # volume's DELETE first — which trips the foreign key with
        # `PRAGMA foreign_keys=ON` and fails the whole request. Ordering the
        # statements is this function's job because it is the one that knows.
        session.flush()
        logger.info("Removed %d volume snapshot row(s) with the volume", len(rows))
    return len(rows)
