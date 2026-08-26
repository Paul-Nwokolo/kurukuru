"""
Snapshot lifecycle, nested under an instance.

Snapshots here are qcow2 *internal* snapshots taken while the instance is
stopped. That is a decision made on measurement rather than convenience — full
VM-state snapshots are refused by WHPX on the reference platform, and the one
live mechanism that does succeed records zero bytes of VM state, which would
restore a filesystem that was never quiesced. The evidence is in
docs/DECISIONS.md; the practical consequence is the 409 below.

Creating and deleting run in the background for the same reason provisioning
does: the work scales with how far the overlay has diverged from its backing
image, and a multi-gigabyte copy must not hold an HTTP worker.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.database import engine as db_engine
from app.database import get_session
from app.engines import (
    ComputeEngine,
    ComputeEngineError,
    EngineRegistry,
    HypervisorUnavailableError,
    UnknownEngineError,
    get_engine_registry,
)
from app.events import record_event
from app.host_capacity import invalidate_cache
from app.models import (
    EventKind,
    Instance,
    InstanceStatus,
    Snapshot,
    SnapshotCreate,
    SnapshotRead,
    SnapshotStatus,
)

logger = logging.getLogger("kurukuru.snapshots")

router = APIRouter(prefix="/instances/{instance_id}/snapshots", tags=["snapshots"])


def _instance_or_404(session: Session, instance_id: str) -> Instance:
    instance = session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")
    return instance


def _snapshot_or_404(session: Session, instance_id: str, snapshot_id: str) -> Snapshot:
    snapshot = session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.instance_id != instance_id:
        raise HTTPException(status_code=404, detail=f"Snapshot '{snapshot_id}' not found")
    return snapshot


def _engine_for(registry: EngineRegistry, instance: Instance) -> ComputeEngine:
    try:
        compute = registry.get(instance.engine)
    except UnknownEngineError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{instance.name}' runs on the retired '{instance.engine}' engine. "
                "It can only be terminated."
            ),
        ) from exc

    if not compute.supports_snapshots:
        raise HTTPException(
            status_code=409,
            detail=f"The '{instance.engine}' engine does not support snapshots",
        )
    return compute


def _require_stopped(compute: ComputeEngine, instance: Instance, action: str) -> None:
    """Refuse the operation unless the instance is stopped, and say why.

    The message names the accelerator limitation rather than stating a rule,
    because "stop it first" invites the reasonable question "why?" — and the
    answer is not something the user did wrong.
    """
    if not compute.snapshots_require_stopped:
        return
    if instance.status is InstanceStatus.STOPPED:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {action} '{instance.name}' while it is {instance.status.value}. "
            "Snapshots capture the disk at rest: this hypervisor cannot save a "
            "running guest's memory, and snapshotting its disk underneath it "
            "would capture a filesystem mid-write. Stop the instance first."
        ),
    )


# --------------------------------------------------------------------------- #
# Background jobs
# --------------------------------------------------------------------------- #
def _create_job(snapshot_id: str) -> None:
    """Take the snapshot and record the outcome on the row."""
    registry = get_engine_registry()

    with Session(db_engine) as session:
        snapshot = session.get(Snapshot, snapshot_id)
        if snapshot is None:  # deleted before the job ran
            return
        instance = session.get(Instance, snapshot.instance_id)
        if instance is None:
            _fail(session, snapshot, "The instance no longer exists")
            return

        try:
            compute = registry.get(instance.engine)
            info = compute.create_snapshot(instance.name, snapshot.name)
        except (ComputeEngineError, UnknownEngineError) as exc:
            logger.error("Snapshot '%s' of '%s' failed: %s", snapshot.name, instance.name, exc)
            _fail(session, snapshot, str(exc))
            return

        snapshot.status = SnapshotStatus.AVAILABLE
        snapshot.size_bytes = info.size_bytes
        snapshot.error_message = None
        session.add(snapshot)
        session.commit()
        # A snapshot's data lives inside the overlay, so free disk has moved.
        invalidate_cache()
        # Recorded on success, not on acceptance: a row that says Creating and
        # then fails should not have announced a snapshot that never existed.
        record_event(
            EventKind.SNAPSHOT_CREATED,
            f"Snapshot '{snapshot.name}' created",
            instance=instance,
            detail=snapshot.description,
        )
        logger.info("Snapshot '%s' of '%s' available", snapshot.name, instance.name)


def _delete_job(snapshot_id: str) -> None:
    """Remove the snapshot from the overlay, then drop the row."""
    registry = get_engine_registry()

    with Session(db_engine) as session:
        snapshot = session.get(Snapshot, snapshot_id)
        if snapshot is None:
            return
        instance = session.get(Instance, snapshot.instance_id)

        if instance is not None:
            try:
                registry.get(instance.engine).delete_snapshot(instance.name, snapshot.name)
            except (ComputeEngineError, UnknownEngineError) as exc:
                logger.error("Deleting snapshot '%s' failed: %s", snapshot.name, exc)
                _fail(session, snapshot, str(exc))
                return

        # Read off the row before it is deleted — afterwards the instance is
        # expired and every attribute access is an error.
        name, owner_id = snapshot.name, snapshot.instance_id
        session.delete(snapshot)
        session.commit()
        invalidate_cache()
        record_event(
            EventKind.SNAPSHOT_DELETED,
            f"Snapshot '{name}' deleted",
            instance=instance,
            instance_id=owner_id,
        )
        logger.info("Deleted snapshot '%s'", name)


def _fail(session: Session, snapshot: Snapshot, message: str) -> None:
    snapshot.status = SnapshotStatus.ERROR
    snapshot.error_message = message
    session.add(snapshot)
    session.commit()


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=SnapshotRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Snapshot an instance (async)",
)
def create_snapshot(
    instance_id: str,
    payload: SnapshotCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Snapshot:
    """Capture the instance's disk. Returns 202; poll the row for progress."""
    instance = _instance_or_404(session, instance_id)
    compute = _engine_for(registry, instance)
    _require_stopped(compute, instance, "snapshot")

    existing = session.exec(
        select(Snapshot)
        .where(Snapshot.instance_id == instance_id)
        .where(Snapshot.name == payload.name)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"'{instance.name}' already has a snapshot named '{payload.name}'",
        )

    snapshot = Snapshot(
        instance_id=instance_id,
        name=payload.name,
        description=payload.description,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    background_tasks.add_task(_create_job, snapshot.id)
    logger.info("Accepted snapshot '%s' of '%s'", snapshot.name, instance.name)
    return snapshot


@router.get("", response_model=list[SnapshotRead], summary="List an instance's snapshots")
def list_snapshots(
    instance_id: str,
    session: Session = Depends(get_session),
) -> list[Snapshot]:
    _instance_or_404(session, instance_id)
    return list(
        session.exec(
            select(Snapshot)
            .where(Snapshot.instance_id == instance_id)
            .order_by(Snapshot.created_at)
        ).all()
    )


@router.post(
    "/{snapshot_id}/restore",
    response_model=SnapshotRead,
    summary="Restore an instance to a snapshot",
)
def restore_snapshot(
    instance_id: str,
    snapshot_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Snapshot:
    """Roll the disk back, discarding everything written since the snapshot.

    Synchronous, unlike create and delete: applying a qcow2 snapshot rewrites
    the L1 table rather than copying data, so it returns in well under a
    second. Backgrounding it would add a status to poll for no benefit.
    """
    instance = _instance_or_404(session, instance_id)
    snapshot = _snapshot_or_404(session, instance_id, snapshot_id)
    compute = _engine_for(registry, instance)
    _require_stopped(compute, instance, "restore")

    if snapshot.status is not SnapshotStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=f"Snapshot '{snapshot.name}' is {snapshot.status.value}, not Available",
        )

    try:
        compute.restore_snapshot(instance.name, snapshot.name)
    except HypervisorUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ComputeEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The event this whole table was argued for. Restore rewrites the disk and
    # changes no row: without this line, the most destructive operation in the
    # product is also the only one that leaves nothing behind.
    record_event(
        EventKind.SNAPSHOT_RESTORED,
        f"Restored to snapshot '{snapshot.name}'",
        instance=instance,
        detail=(
            f"Everything written to the disk since {snapshot.created_at:%Y-%m-%d %H:%M:%S} "
            "UTC was discarded."
        ),
    )
    logger.info("Restored '%s' to snapshot '%s'", instance.name, snapshot.name)
    return snapshot


@router.delete(
    "/{snapshot_id}",
    response_model=SnapshotRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Delete a snapshot (async)",
)
def delete_snapshot(
    instance_id: str,
    snapshot_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Snapshot:
    """Remove a snapshot. Returns 202; the row disappears when it is gone."""
    instance = _instance_or_404(session, instance_id)
    snapshot = _snapshot_or_404(session, instance_id, snapshot_id)
    compute = _engine_for(registry, instance)
    _require_stopped(compute, instance, "delete a snapshot of")

    snapshot.status = SnapshotStatus.DELETING
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)

    background_tasks.add_task(_delete_job, snapshot.id)
    return snapshot


def delete_snapshots_for_instance(session: Session, instance_id: str) -> int:
    """Drop every snapshot row for an instance. Returns how many.

    Called when an instance is terminated. No hypervisor work is needed — the
    snapshots live *inside* the overlay, and the overlay goes with the instance
    directory. Leaving the rows behind would advertise restore points whose
    data was deleted seconds earlier.
    """
    rows = session.exec(select(Snapshot).where(Snapshot.instance_id == instance_id)).all()
    for row in rows:
        session.delete(row)
    if rows:
        logger.info("Removed %d snapshot row(s) with the instance", len(rows))
    return len(rows)
