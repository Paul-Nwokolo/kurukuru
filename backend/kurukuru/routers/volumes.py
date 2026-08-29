"""
Additional disks.

A volume is a blank qcow2 file that can be attached to an instance and, more
importantly, **outlives it**. That is the whole reason the feature exists, and
it drives the two rules that matter most here:

**Terminating an instance never deletes its volumes.** They are detached and
left ``Available``, data intact. Losing someone's data because they destroyed
the wrong VM is the one unforgivable failure in this module, so the detach is
done by :func:`detach_volumes_for_instance` — called from the terminate route
*before* the instance row is cleared — and it is tested directly.

**Attach and detach require a stopped instance.** Not for convenience: QEMU's
hot-unplug was measured against a mounted filesystem and removed the device in
under a second while reporting success, leaving the guest with an aborted
journal and `Input/output error`. Hot-*plug* additionally needs PCIe root ports
reserved at boot, which no instance this product has ever launched has. The
evidence is in DECISIONS #22; the practical consequence is the 409 below.

Order is load-bearing. ``attach_order`` decides both the position of the
``-drive`` argument and the guest's device name, so it is assigned once at
attach and compacted on detach, never recomputed from a set.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlmodel import Session, select

from kurukuru.config import Settings, get_settings
from kurukuru.database import engine as db_engine
from kurukuru.database import get_session
from kurukuru.engines import (
    ComputeEngineError,
    EngineRegistry,
    UnknownEngineError,
    get_engine_registry,
)
from kurukuru.events import record_event
from kurukuru.host_capacity import get_capacity, invalidate_cache
from kurukuru.models import (
    EventKind,
    Instance,
    InstanceStatus,
    Volume,
    VolumeAttach,
    VolumeCreate,
    VolumeRead,
    VolumeStatus,
)
from kurukuru.routers.projects import project_filter, resolve_project_id

logger = logging.getLogger("kurukuru.volumes")

router = APIRouter(prefix="/volumes", tags=["volumes"])


def volumes_dir(settings: Settings) -> Path:
    return Path(settings.qemu_dir).expanduser() / "volumes"


def volume_path(volume: Volume, settings: Settings) -> Path:
    """Where a volume's file lives.

    Named by **uuid**, not by the volume's name. A volume name is a label the
    user can reuse across projects because nothing outside this database refers
    to the file — the opposite of an instance name, which *is* the hypervisor's
    identity and therefore has to be globally unique (DECISIONS #20 and #22).
    """
    return volumes_dir(settings) / f"{volume.id}.qcow2"


def _get_or_404(session: Session, volume_id: str) -> Volume:
    volume = session.get(Volume, volume_id)
    if volume is None:
        raise HTTPException(status_code=404, detail=f"Volume '{volume_id}' not found")
    return volume


def _read(session: Session, volume: Volume) -> VolumeRead:
    attached_to = None
    guest_os = None
    if volume.attached_instance_id:
        instance = session.get(Instance, volume.attached_instance_id)
        if instance is not None:
            # The guest's OS travels with the attachment because the device
            # name a user should go looking for depends on it — "/dev/vdb" on
            # Linux, "Disk 1" in Windows Disk Management. Resolved here, where
            # the instance row is already loaded, rather than making every
            # client fetch the instance list to render one column.
            attached_to, guest_os = instance.name, instance.guest_os
    return VolumeRead(
        **volume.model_dump(),
        attached_instance_name=attached_to,
        attached_instance_guest_os=guest_os,
    )


def _attached_paths(session: Session, instance_id: str) -> list[str]:
    """Every volume attached to an instance, in attach order.

    The single source of the ordering. Anything that changes an attachment
    calls this and hands the result to the engine, so the database and the
    runtime file cannot disagree about which disk is which.
    """
    volumes = session.exec(
        select(Volume)
        .where(Volume.attached_instance_id == instance_id)
        .order_by(Volume.attach_order)
    ).all()
    return [v.path for v in volumes]


def _push_to_engine(
    session: Session, registry: EngineRegistry, instance: Instance
) -> None:
    """Tell the driver what this instance boots with now."""
    try:
        compute = registry.get(instance.engine)
    except UnknownEngineError:
        return  # a retired-engine row; it can only be terminated anyway
    if not compute.supports_volumes:
        return
    try:
        compute.set_volumes(instance.name, _attached_paths(session, instance.id))
    except ComputeEngineError as exc:
        # The database already says what the user asked for, and the next start
        # re-reads it. Failing the request here would leave the two disagreeing
        # in the other direction, which is worse.
        logger.warning("Could not update volumes for '%s': %s", instance.name, exc)


# --------------------------------------------------------------------------- #
# Background creation
# --------------------------------------------------------------------------- #
def _create_job(volume_id: str) -> None:
    """Allocate the qcow2 file and mark the row Available."""
    from kurukuru.engines.qemu import QemuEngine

    settings = get_settings()
    with Session(db_engine) as session:
        volume = session.get(Volume, volume_id)
        if volume is None:  # deleted before the job ran
            return

        path = volume_path(volume, settings)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            QemuEngine(settings).create_blank_disk(path, volume.size_gb)
        except (ComputeEngineError, OSError) as exc:
            logger.error("Creating volume '%s' failed: %s", volume.name, exc)
            volume.status = VolumeStatus.ERROR
            volume.error_message = str(exc)
            session.add(volume)
            session.commit()
            return

        volume.path = str(path)
        volume.status = VolumeStatus.AVAILABLE
        volume.error_message = None
        session.add(volume)
        session.commit()
        invalidate_cache()  # a new file on the instance volume
        logger.info("Volume '%s' available (%d GB)", volume.name, volume.size_gb)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=VolumeRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Create a blank volume (async)",
)
def create_volume(
    payload: VolumeCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> VolumeRead:
    """Allocate a blank qcow2 of the requested size. Returns 202.

    The disk is **unformatted**. Nothing here partitions or formats it, and
    nothing mounts it — that is the guest's business, and guessing at a
    filesystem for someone would be a decision this system has no basis to
    make. The API docs and the UI say what to run.
    """
    project_id = resolve_project_id(session, payload.project_id)

    clash = session.exec(
        select(Volume)
        .where(Volume.name == payload.name)
        .where(Volume.project_id == project_id)
    ).first()
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A volume named '{payload.name}' already exists in this project",
        )

    # Checked against real free space, exactly as an instance disk is. A qcow2
    # is sparse, so this refuses on the *promise* rather than on what is
    # consumed today — the honest reading, since the guest may fill it.
    capacity = get_capacity(session, settings)
    if payload.size_gb > capacity.disk_gb.allocatable:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Requested {payload.size_gb} GB but only "
                f"{capacity.disk_gb.allocatable} GB is free on the volume that "
                f"holds instance storage."
            ),
        )

    volume = Volume(
        name=payload.name,
        size_gb=payload.size_gb,
        project_id=project_id,
    )
    session.add(volume)
    session.commit()
    session.refresh(volume)

    background_tasks.add_task(_create_job, volume.id)
    logger.info("Accepted volume '%s' (%d GB)", volume.name, volume.size_gb)
    return _read(session, volume)


@router.get("", response_model=list[VolumeRead], summary="List volumes")
def list_volumes(
    project_id: str | None = Depends(project_filter),
    instance_id: str | None = Query(None, description="Only volumes attached to this instance"),
    session: Session = Depends(get_session),
) -> list[VolumeRead]:
    stmt = select(Volume)
    if project_id is not None:
        stmt = stmt.where(Volume.project_id == project_id)
    if instance_id is not None:
        stmt = stmt.where(Volume.attached_instance_id == instance_id)
    volumes = session.exec(stmt.order_by(Volume.created_at)).all()
    return [_read(session, volume) for volume in volumes]


@router.get("/{volume_id}", response_model=VolumeRead, summary="Get one volume")
def get_volume(volume_id: str, session: Session = Depends(get_session)) -> VolumeRead:
    return _read(session, _get_or_404(session, volume_id))


@router.post(
    "/{volume_id}/attach",
    response_model=VolumeRead,
    summary="Attach a volume to a stopped instance",
)
def attach_volume(
    volume_id: str,
    payload: VolumeAttach,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> VolumeRead:
    """Attach. Takes effect on the instance's next start.

    Refused while the instance is running — see the module docstring and
    DECISIONS #22 for what hot-plug actually does to a mounted filesystem.
    """
    volume = _get_or_404(session, volume_id)
    instance = session.get(Instance, payload.instance_id)
    if instance is None:
        raise HTTPException(
            status_code=404, detail=f"Instance '{payload.instance_id}' not found"
        )

    if volume.status is VolumeStatus.ATTACHED:
        current = session.get(Instance, volume.attached_instance_id or "")
        where = f" to '{current.name}'" if current is not None else ""
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{volume.name}' is already attached{where}. A volume can only "
                f"be attached to one instance at a time — two guests writing to "
                f"one filesystem would corrupt it."
            ),
        )
    if volume.status is not VolumeStatus.AVAILABLE:
        raise HTTPException(
            status_code=409,
            detail=f"'{volume.name}' is {volume.status.value}, not Available",
        )
    _require_stopped(instance, "attach a volume to")

    used = {
        v.attach_order
        for v in session.exec(
            select(Volume).where(Volume.attached_instance_id == instance.id)
        ).all()
        if v.attach_order is not None
    }
    volume.attach_order = max(used) + 1 if used else 0
    volume.attached_instance_id = instance.id
    volume.status = VolumeStatus.ATTACHED
    session.add(volume)
    session.commit()
    session.refresh(volume)

    _push_to_engine(session, registry, instance)
    record_event(
        EventKind.VOLUME_ATTACHED,
        f"Volume '{volume.name}' attached ({volume.size_gb} GB)",
        instance=instance,
        detail=(
            "Takes effect on the next start. The disk is unformatted — in the "
            "guest, run `lsblk` to find it, then `mkfs.ext4` and `mount`."
        ),
    )
    return _read(session, volume)


@router.post(
    "/{volume_id}/detach",
    response_model=VolumeRead,
    summary="Detach a volume from its instance",
)
def detach_volume(
    volume_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> VolumeRead:
    """Detach. The volume keeps its data and becomes Available."""
    volume = _get_or_404(session, volume_id)
    if volume.status is not VolumeStatus.ATTACHED or not volume.attached_instance_id:
        raise HTTPException(
            status_code=409, detail=f"'{volume.name}' is not attached to anything"
        )

    instance = session.get(Instance, volume.attached_instance_id)
    if instance is not None:
        _require_stopped(instance, "detach a volume from")

    _detach(session, volume)
    session.commit()
    session.refresh(volume)

    if instance is not None:
        _compact_order(session, instance.id)
        session.commit()
        _push_to_engine(session, registry, instance)
        record_event(
            EventKind.VOLUME_DETACHED,
            f"Volume '{volume.name}' detached",
            instance=instance,
            detail="The volume and its data are untouched; it is now Available.",
        )
    return _read(session, volume)


@router.delete(
    "/{volume_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a volume and its data",
)
def delete_volume(
    volume_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Delete the volume **and its file**. Refused while attached.

    This is the one operation in this module that destroys data, which is why
    it is also the only one that cannot be reached by accident: an attached
    volume must be detached first, deliberately, from a stopped instance.
    """
    volume = _get_or_404(session, volume_id)
    if volume.status is VolumeStatus.ATTACHED:
        instance = session.get(Instance, volume.attached_instance_id or "")
        where = f" to '{instance.name}'" if instance is not None else ""
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{volume.name}' is still attached{where}. Detach it first — "
                f"deleting a disk out from under an instance would leave it "
                f"unable to start."
            ),
        )

    path = Path(volume.path) if volume.path else volume_path(volume, settings)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not delete {path}: {exc}"
        ) from exc

    # The snapshots lived inside the file that was just unlinked, so the rows
    # have to go with it — leaving them would advertise restore points whose
    # data is already gone. Imported here rather than at module scope because
    # volume_snapshots imports volume_path from this module.
    from kurukuru.routers.volume_snapshots import delete_snapshots_for_volume

    dropped = delete_snapshots_for_volume(session, volume.id)
    session.delete(volume)
    session.commit()
    invalidate_cache()
    logger.info("Deleted volume '%s' and %s (%d snapshot row(s))",
                volume.name, path, dropped)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _require_stopped(instance: Instance, action: str) -> None:
    """Refuse unless the instance is stopped, and say why.

    The message names the measurement rather than stating a rule, because
    "stop it first" invites "why?" and the honest answer is that this
    hypervisor's hot-unplug does not ask the guest before pulling the disk.
    """
    if instance.status is InstanceStatus.STOPPED:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Cannot {action} '{instance.name}' while it is "
            f"{instance.status.value}. Volumes are attached at boot: this "
            f"hypervisor removes a hot-unplugged disk without waiting for the "
            f"guest, which corrupts a mounted filesystem. Stop the instance "
            f"first."
        ),
    )


def _detach(session: Session, volume: Volume) -> None:
    """Clear an attachment in place. Never touches the file."""
    volume.attached_instance_id = None
    volume.attach_order = None
    volume.status = VolumeStatus.AVAILABLE
    session.add(volume)


def _compact_order(session: Session, instance_id: str) -> None:
    """Renumber an instance's remaining volumes to 0..n-1.

    Without this, detaching the first of three volumes would leave orders
    1 and 2, and the *next* attach would take 3 — harmless for the drive
    argument order, but it would make ``device_hint`` claim ``vde`` for what
    the guest will call ``vdc``. The hint has to match the arithmetic the
    engine actually performs.
    """
    remaining = session.exec(
        select(Volume)
        .where(Volume.attached_instance_id == instance_id)
        .order_by(Volume.attach_order)
    ).all()
    for index, volume in enumerate(remaining):
        if volume.attach_order != index:
            volume.attach_order = index
            session.add(volume)


def detach_volumes_for_instance(session: Session, instance_id: str) -> list[str]:
    """Detach every volume from an instance. Returns their names.

    Called when an instance is terminated, and the reason this function exists
    rather than being inlined: **terminating an instance must never delete a
    volume**. The instance's own overlay and its snapshots go with it — those
    live inside its directory — but a volume is a separate file that was
    attached to it, often holding the only copy of something. Detaching leaves
    it ``Available`` with its data intact, which is both the AWS behaviour and
    the only safe one.
    """
    volumes = session.exec(
        select(Volume).where(Volume.attached_instance_id == instance_id)
    ).all()
    for volume in volumes:
        _detach(session, volume)
    if volumes:
        logger.info(
            "Detached %d volume(s) from the terminated instance; data left intact",
            len(volumes),
        )
    return [volume.name for volume in volumes]
