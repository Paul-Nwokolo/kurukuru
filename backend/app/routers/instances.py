"""
Instance lifecycle API.

The database is the *desired* state: every mutating route writes the DB first,
then drives the hypervisor. Actual hypervisor state is folded back in by the
reconciler (:func:`reconcile_all` / :func:`_sync_instance`), which fills in IPs,
corrects statuses and marks rows ``Error`` when a VM has vanished.

Provisioning is non-blocking: ``POST /instances`` returns ``202`` immediately
with a ``Pending`` record and runs the (up-to-600s) launch in a background task.

Every row carries the ``engine`` that owns it, and each route resolves its
driver from that column via the :class:`EngineRegistry`. The routes themselves
stay engine-agnostic: a QEMU VM and a Multipass VM are started, stopped and
terminated through exactly the same endpoints.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, WebSocket, status
from sqlmodel import Session, select

from app.cloud_init import CloudInitError, build_cloud_init
from app.cloud_init import cleanup as cleanup_cloud_init
from app.config import Settings, get_settings
from app.console import (
    CLOSE_CONFLICT,
    CLOSE_NOT_FOUND,
    bridge_websocket_to_vnc,
)
from app.engines.qemu import HOST_IP
from app.database import engine as db_engine
from app.database import get_session
from app.events import record_event
from app.engines import (
    ComputeEngine,
    ComputeEngineError,
    EngineRegistry,
    HypervisorUnavailableError,
    InstanceInfo,
    LaunchOptions,
    UnknownEngineError,
    get_engine_registry,
)
from app.host_capacity import HostCapacity, get_capacity, invalidate_cache
from app.image_store import image_path
from app.isos import IsoError, resolve_iso
from app.models import (
    CUSTOM_PRESET,
    DEFAULT_ACCEL,
    BootSource,
    EventActor,
    EventKind,
    PROVISIONABLE_GUEST_OS,
    Image,
    ImageSource,
    ImageStatus,
    Instance,
    InstanceClone,
    InstanceCreate,
    Project,
    InstanceKeyPair,
    InstanceKeyPairRead,
    InstanceRead,
    InstanceStatus,
    KeyPair,
    _utcnow,
)
from app.routers.keypairs import orchestrator_keypair
from app.routers.projects import project_filter, resolve_project_id
from app.user_data import UserDataError, parse_user_data

logger = logging.getLogger("iaas.instances")

router = APIRouter(prefix="/instances", tags=["instances"])

# Statuses from which start/stop are refused (terminal for those transitions).
_TERMINAL_STATUSES = {InstanceStatus.TERMINATED, InstanceStatus.ERROR}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _touch(instance: Instance) -> None:
    instance.updated_at = _utcnow()


def _get_or_404(session: Session, instance_id: str) -> Instance:
    instance = session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")
    return instance


def _resolve_launch_image(session: Session, image_id: str | None) -> Image | None:
    """Pick the image to back a new instance, defaulting to the built-in one.

    Returns None only when the catalog has no built-in row yet (a fresh install
    whose base image hasn't been downloaded), in which case the engine falls
    back to its own default and the row simply records no image.
    """
    if image_id:
        image = session.get(Image, image_id)
        if image is None:
            raise HTTPException(status_code=422, detail=f"Unknown image '{image_id}'")
        if not _image_is_launchable(image):
            raise HTTPException(
                status_code=409,
                detail=f"Image '{image.name}' is {image.status.value}, not Available",
            )
        return image

    return session.exec(
        select(Image).where(Image.source == ImageSource.BUILTIN)
    ).first()


def _resolve_network_id(session: Session, network_id: str | None) -> str | None:
    """Validate a requested network, or fall back to the default one.

    Only user mode ships, so there is exactly one network to be on — but the
    field is honoured rather than ignored so a client written against a future
    build gets a 422 naming the problem instead of silent placement.
    """
    from app.models import Network
    from app.routers.networks import default_network

    if network_id is None:
        fallback = default_network(session)
        return fallback.id if fallback is not None else None
    if session.get(Network, network_id) is None:
        raise HTTPException(status_code=422, detail=f"Unknown network '{network_id}'")
    return network_id


def _resolve_keypairs(
    session: Session, keypair_ids: list[str] | None
) -> list[KeyPair]:
    """Which keys go into this guest's authorized_keys.

    ``None`` means "you decide", and the answer is the orchestrator key — the
    behaviour every launch had before key pairs existed. An explicit ``[]``
    means "none", which is a legitimate request for a console-only instance and
    is honoured rather than quietly corrected.
    """
    if keypair_ids is None:
        default = orchestrator_keypair(session)
        return [default] if default is not None else []

    resolved: list[KeyPair] = []
    for keypair_id in keypair_ids:
        keypair = session.get(KeyPair, keypair_id)
        if keypair is None:
            raise HTTPException(
                status_code=422, detail=f"Unknown key pair '{keypair_id}'"
            )
        if keypair.id not in {k.id for k in resolved}:
            resolved.append(keypair)
    return resolved


def _record_keypairs(session: Session, instance: Instance, keypairs: list[KeyPair]) -> None:
    """Link an instance to the keys it launched with.

    Name and fingerprint are copied onto the link on purpose: the keypair row
    may be deleted later, and the guest will still have that key in its
    authorized_keys. The association is a record of what happened, so it has to
    survive the thing it refers to.
    """
    for keypair in keypairs:
        session.add(
            InstanceKeyPair(
                instance_id=instance.id,
                keypair_id=keypair.id,
                keypair_name=keypair.name,
                fingerprint=keypair.fingerprint,
            )
        )


class NoUsableKeysError(RuntimeError):
    """Every key pair an instance was launched with has since been deleted."""


def instance_public_keys(session: Session, instance: Instance) -> list[str]:
    """Public keys to install, resolved from the instance's recorded links.

    Read at provisioning time rather than passed down from the request, so a
    restart of a half-built instance installs the same keys the row says it has.

    Losing *some* keys between launch and provisioning is survivable: the rest
    still work, and refusing the launch would be a worse outcome than a guest
    reachable by fewer keys than intended. Losing *all* of them is not — the
    guest would boot with an empty ``authorized_keys``, report ``ssh_enabled``,
    publish an address, and refuse every connection made to it. That is the
    same class of failure as cloud-init not rendering, so it gets the same
    treatment: fail the provision with a message that says what happened.

    An instance launched with no key pairs at all is a different thing entirely
    — a deliberate console-only guest — and passes straight through.
    """
    links = session.exec(
        select(InstanceKeyPair).where(InstanceKeyPair.instance_id == instance.id)
    ).all()

    keys: list[str] = []
    missing: list[str] = []
    for link in links:
        keypair = session.get(KeyPair, link.keypair_id)
        if keypair is not None:
            keys.append(keypair.public_key)
        else:
            missing.append(link.keypair_name or link.keypair_id)

    if links and not keys:
        raise NoUsableKeysError(
            f"Every key pair this instance was launched with has been deleted "
            f"({', '.join(missing)}). Provisioning it now would produce a guest "
            f"with no way in. Launch again with a key pair that still exists."
        )
    if missing:
        logger.warning(
            "Key pair(s) %s for instance '%s' no longer exist; installing the "
            "remaining %d", ", ".join(missing), instance.name, len(keys),
        )
    return keys


def _validate_user_data(user_data: str | None, boot_source: BootSource) -> None:
    """Refuse user-data that cannot work, at the request rather than at boot.

    Two ways it cannot work. Invalid YAML is refused with the parser's own
    message — line and column included — because "invalid YAML" on a fifty-line
    document leaves the user nowhere to start. And an ISO instance has no
    cloud-init at all: accepting a cloud-config for one would silently discard
    it and leave the user waiting for packages that were never going to install.
    """
    if user_data is None or not user_data.strip():
        return

    if boot_source is BootSource.ISO:
        raise HTTPException(
            status_code=422,
            detail=(
                "user-data cannot be used with an ISO instance: an installer has "
                "no cloud-init to read it. Launch from a cloud image instead."
            ),
        )

    try:
        parse_user_data(user_data)
    except UserDataError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _image_is_launchable(image: Image) -> bool:
    """Whether an instance can be built on this image *now*.

    ``Available`` means the file is in the store. The built-in image is the one
    exception, and getting this wrong broke the first launch on every fresh
    install: its file is downloaded **on demand** by the engine
    (``ensure_base_image``), so it sits at ``Importing`` — "not downloaded yet"
    — until something asks for it. Requiring ``Available`` there meant the
    first launch was refused for the absence of a file that the launch itself
    was supposed to fetch, and the second launch worked because a *previous*
    error had left the file behind. It presents as "Image is Importing, not
    Available" on a brand-new install and nowhere else, which is why it
    survived nine phases of a developer machine that had already downloaded it.

    ``Error`` still refuses, for the built-in image too: that means the file is
    present and unreadable, which no amount of downloading fixes.
    """
    if image.status is ImageStatus.AVAILABLE:
        return True
    return image.source is ImageSource.BUILTIN and image.status is ImageStatus.IMPORTING


def _image_minimum_disk_gb(image: Image | None) -> int | None:
    """Smallest overlay that can sit on this image, rounded up.

    A qcow2 overlay reports its own size to the guest, so one smaller than its
    backing file would present a disk shorter than the filesystem already on it.
    """
    if image is None or not image.virtual_size_bytes:
        return None
    return math.ceil(image.virtual_size_bytes / (1024**3))


@dataclass(frozen=True)
class ResolvedSizing:
    """The hardware a launch request actually asks for, plus its label."""

    cpus: int
    memory_mb: int
    disk_gb: int
    label: str


def _resolve_sizing(payload: InstanceCreate, settings: Settings) -> ResolvedSizing:
    """Turn a request's preset and/or explicit numbers into one shape.

    A preset supplies defaults; any explicit field overrides it. That ordering
    is what makes "Large, but with more disk" expressible without inventing a
    preset for it — and it means the label records where the user started, not
    what they ended up with.
    """
    preset_name = (payload.preset or payload.flavor or "").strip().lower()
    spec = settings.flavors.get(preset_name)
    if preset_name and spec is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown preset '{preset_name}'. Known presets: "
                f"{', '.join(settings.flavors)}."
            ),
        )
    if spec is None:  # no preset named at all — fall back to the first one
        preset_name, spec = next(iter(settings.flavors.items()))

    cpus = payload.cpus if payload.cpus is not None else spec.cpus
    memory_mb = payload.memory_mb if payload.memory_mb is not None else spec.memory_mb
    disk_gb = payload.disk_gb if payload.disk_gb is not None else spec.disk_gb

    customised = (cpus, memory_mb, disk_gb) != (spec.cpus, spec.memory_mb, spec.disk_gb)
    return ResolvedSizing(
        cpus=cpus,
        memory_mb=memory_mb,
        disk_gb=disk_gb,
        label=CUSTOM_PRESET if customised else preset_name,
    )


def _validate_sizing(
    sizing: ResolvedSizing,
    capacity: HostCapacity,
    settings: Settings,
    *,
    minimum_disk_gb: int | None = None,
) -> None:
    """Refuse a request the host cannot satisfy, saying why in numbers.

    Server-side and authoritative: the UI shows the same limits, but a launch
    that the hypervisor would fail on must be refused here, with the arithmetic
    spelled out — "too big" is not actionable, "12288 MB requested, 9216 MB
    allocatable (2048 reserve, 4096 committed)" is.
    """
    reserve_mb = settings.host_reserve_memory_bytes // (1024**2)

    if sizing.cpus < settings.min_instance_cpus:
        raise HTTPException(
            status_code=422,
            detail=f"An instance needs at least {settings.min_instance_cpus} vCPU.",
        )
    if sizing.memory_mb < settings.min_instance_memory_mb:
        raise HTTPException(
            status_code=422,
            detail=f"An instance needs at least {settings.min_instance_memory_mb} MB of memory.",
        )
    if sizing.disk_gb < settings.min_instance_disk_gb:
        raise HTTPException(
            status_code=422,
            detail=f"An instance needs at least {settings.min_instance_disk_gb} GB of disk.",
        )

    # A capacity probe that failed must not block launches (see host_capacity).
    if capacity.degraded:
        logger.warning("Capacity limits not enforced: %s", "; ".join(capacity.warnings))
        return

    if sizing.cpus > capacity.cpu.max_per_instance:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Requested {sizing.cpus} vCPUs but only {capacity.cpu.max_per_instance} "
                f"are allocatable ({capacity.cpu.total} host cores, "
                f"{capacity.cpu.committed} committed to running instances)."
            ),
        )
    if sizing.memory_mb > capacity.memory_mb.allocatable:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Requested {sizing.memory_mb} MB but only "
                f"{capacity.memory_mb.allocatable} MB allocatable "
                f"({reserve_mb} MB host reserve, {capacity.memory_mb.committed} MB "
                f"committed to running instances)."
            ),
        )
    if sizing.disk_gb > capacity.disk_gb.allocatable:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Requested {sizing.disk_gb} GB but only "
                f"{capacity.disk_gb.allocatable} GB free on the disk holding the "
                "instance store."
            ),
        )

    # A qcow2 overlay cannot be smaller than what it is layered on: the guest
    # would see a disk shorter than its own filesystem.
    if minimum_disk_gb is not None and sizing.disk_gb < minimum_disk_gb:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Disk must be at least {minimum_disk_gb} GB — the size of the "
                f"selected image. Growing a disk is fine; shrinking is not."
            ),
        )


def _engine_for(registry: EngineRegistry, instance: Instance) -> ComputeEngine:
    """Resolve the driver that owns this row.

    A row can name an engine that no longer ships (Multipass rows survive its
    retirement). There is nothing to drive for those, and no amount of retrying
    helps, so the refusal is a 409 naming the engine rather than a 500 — the VM
    is gone with its hypervisor, and the only remaining action is to terminate
    the row, which :func:`delete_instance` handles without a driver.
    """
    try:
        return registry.get(instance.engine)
    except UnknownEngineError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{instance.name}' runs on the retired '{instance.engine}' engine, "
                "which is no longer supported. The instance can only be terminated."
            ),
        ) from exc


def _row_snapshot(instance: Instance) -> tuple:
    """Everything reconciliation is allowed to change — for change detection."""
    return (
        instance.status,
        instance.ip_address,
        instance.error_message,
        instance.ssh_port,
        instance.vnc_port,
        instance.qmp_port,
        instance.pid,
    )


#: Field order for :func:`_row_snapshot`, so a diff can name what changed.
_SNAPSHOT_FIELDS = (
    "status", "ip_address", "error_message", "ssh_port", "vnc_port", "qmp_port", "pid",
)


#: Instances with an API operation currently running against the hypervisor,
#: and a count of completed operations per instance. Guarded by a lock: the
#: reconciler runs on a worker thread while routes run on the threadpool.
_in_flight: set[str] = set()
_operations_completed: dict[str, int] = {}
_in_flight_lock = threading.Lock()


def _operation_state() -> tuple[set[str], dict[str, int]]:
    """A consistent snapshot of what this process is doing, and has just done."""
    with _in_flight_lock:
        return set(_in_flight), dict(_operations_completed)


@contextmanager
def _api_operation(instance_id: str) -> Iterator[None]:
    """Claim an instance while this process drives its hypervisor.

    Start, stop, provisioning and terminate all block on QEMU for tens of
    seconds, and the reconciler runs every 30. So the background pass routinely
    observes an operation that has not returned yet. Two things went wrong
    because of that, and this fixes both.

    The visible one: a launch grew a "Corrected to Running (record said
    Provisioning)" line and a start grew "Corrected to Running (record said
    Stopped)", on every single one. Nothing was corrected — the reconciler
    simply saw our own work and had no way to know it was ours. A log that is
    mostly noise stops being read.

    The one underneath it: :func:`reconcile_all` lists the hypervisor *before*
    it reads the rows, so a stop that lands in that window is overwritten by a
    listing taken when the VM was still up. The row flips back to Running a
    second after ``stop`` returned, and the next ``start`` is refused with "only
    Stopped instances can be started". Found live, chasing the noisy line above.

    So a claim now suppresses the write as well as the event, for the whole
    window: claimed when the listing began, claimed when the write happens, or
    an operation that began and finished in between (which the counter catches
    and the two set snapshots would not).

    Deliberately in-process and advisory. It is not a lock and guarantees
    nothing; a missed claim costs one stale reconcile, exactly as before this
    existed. Nothing in here is allowed to fail.
    """
    with _in_flight_lock:
        _in_flight.add(instance_id)
    try:
        yield
    finally:
        with _in_flight_lock:
            _in_flight.discard(instance_id)
            _operations_completed[instance_id] = (
                _operations_completed.get(instance_id, 0) + 1
            )


def _describe_correction(before: tuple, after: tuple) -> tuple[str, str]:
    """Summarise a reconciler correction as (summary, detail).

    The summary leads with status when status moved, because that is the change
    a person cares about; everything else is a detail line. The detail records
    both sides — "what it observed and what it changed" is only useful if the
    previous value is in it, since nothing else keeps that.
    """
    changes = [
        (field, old, new)
        for field, old, new in zip(_SNAPSHOT_FIELDS, before, after, strict=True)
        if old != new
    ]

    def _render(value: object) -> str:
        if value is None:
            return "none"
        return getattr(value, "value", value).__str__()

    status_change = next((c for c in changes if c[0] == "status"), None)
    if status_change is not None:
        summary = (
            f"Corrected to {_render(status_change[2])} "
            f"(record said {_render(status_change[1])})"
        )
    else:
        summary = "Runtime detail corrected: " + ", ".join(c[0] for c in changes)

    detail = "\n".join(
        f"{field}: {_render(old)} -> {_render(new)}" for field, old, new in changes
    )
    return summary, detail


def _apply_info(
    instance: Instance, info: InstanceInfo, *, authoritative: bool = False
) -> None:
    """Fold a hypervisor snapshot into a DB row (in place, no commit).

    ``authoritative`` marks a caller that *knows* provisioning has finished —
    only the provisioning job does. A background reconcile pass does not, and
    must not promote a half-built VM out of Provisioning.
    """
    if not info.exists:
        # DB thinks it should exist but the hypervisor disagrees.
        if instance.status in (InstanceStatus.PENDING, InstanceStatus.PROVISIONING):
            # Still being launched — the VM may simply not be listed yet.
            return
        instance.status = InstanceStatus.ERROR
        instance.error_message = "VM no longer exists on the hypervisor"
        _touch(instance)
        return

    if not authoritative and instance.status in (
        InstanceStatus.PENDING,
        InstanceStatus.PROVISIONING,
    ):
        # A launch in flight belongs to the provisioning job. Half-built VMs pass
        # through states that look meaningful in isolation, so a reconcile pass
        # landing mid-launch would announce Running before the guest is usable —
        # and, with no address published yet, strand the row on "Running, no IP".
        #
        # Note the deliberate asymmetry: an address is evidence of readiness but
        # not a requirement for it. An ISO guest never publishes one, which is
        # why only the job — which knows the launch returned — can finish the
        # transition for those.
        if not (info.status is InstanceStatus.RUNNING and info.ip_address):
            return

    changed = False
    if info.status is not None and instance.status != info.status:
        instance.status = info.status
        changed = True
    if info.ip_address and instance.ip_address != info.ip_address:
        instance.ip_address = info.ip_address
        changed = True
    if instance.status == InstanceStatus.RUNNING and instance.error_message:
        instance.error_message = None
        changed = True

    # Engine runtime detail. Ports are pinned for the instance's life, so once
    # reported they are never cleared — only the pid comes and goes with the
    # VM process, and a stopped VM must still show the port you'd SSH to.
    for field in ("ssh_port", "vnc_port", "qmp_port", "accel", "display", "ssh_enabled"):
        value = getattr(info, field)
        if value is not None and getattr(instance, field) != value:
            setattr(instance, field, value)
            changed = True
    if instance.pid != info.pid:
        instance.pid = info.pid
        changed = True

    if changed:
        _touch(instance)


def _sync_instance(
    session: Session,
    compute: ComputeEngine,
    instance: Instance,
    *,
    authoritative: bool = False,
) -> None:
    """Reconcile a single instance against the hypervisor and commit."""
    info = compute.get_instance_info(instance.name)
    _apply_info(instance, info, authoritative=authoritative)
    session.add(instance)
    session.commit()
    session.refresh(instance)


def reconcile_all(session: Session, registry: EngineRegistry) -> int:
    """Reconcile every non-terminated instance. Returns the number updated.

    Dispatches per engine: one bulk listing call per *distinct engine actually
    in use* (never for engines nothing is running on), then matches each row
    against the listing from its own engine, so two drivers' VMs sharing a name
    can't shadow each other.

    The pass is deliberately three phases — decide which engines to ask, do the
    slow listing with no transaction open, then read-decide-write in one
    boundary. Reading rows *after* the I/O is what makes the write safe: a
    terminate that lands mid-pass is already visible by the time we decide, so
    no stale snapshot can overwrite it, and no per-row re-read is needed.

    Rows naming a **retired** engine are skipped outright. There is no driver to
    ask about them, and treating "no driver" like "VM missing" would mark every
    historical Multipass row Error on the first pass after the retirement —
    rewriting history to say those instances failed, when in fact they ran fine
    and their hypervisor was removed underneath them. They keep their last known
    state until someone terminates them.

    Terminated rows are excluded, which is what makes name reuse safe: a name
    identifies at most one *live* row (enforced by :func:`create_instance`), so
    matching a hypervisor listing by name can never hit a stale audit row from
    an earlier instance that happened to use the same name.

    One engine being down must not blind the reconciler to the others, so a
    failing listing is logged and its rows are left untouched this pass.
    """
    # Phase 1 — decide which engines to ask, from a cheap read.
    engines_in_use = {
        row.engine
        for row in session.exec(
            select(Instance).where(Instance.status != InstanceStatus.TERMINATED)
        ).all()
    }

    # Phase 2 — the slow part, with no transaction held open. Listing a
    # hypervisor takes seconds (and up to a full timeout when one is wedged);
    # holding rows across that is what created the write-back race in Phase 6.
    #
    # What this process was doing when the listing began. Anything it was
    # driving is described by evidence that may already be out of date — see
    # _api_operation.
    busy_at_start, operations_at_start = _operation_state()
    listings: dict[str, dict[str, InstanceInfo]] = {}
    for engine_name in engines_in_use:
        if not registry.supports(engine_name):
            logger.debug("Reconcile: skipping rows on retired engine '%s'", engine_name)
            continue
        try:
            listings[engine_name] = registry.get(engine_name).list_instances()
        except ComputeEngineError as exc:
            logger.warning("Reconcile: engine '%s' listing failed: %s", engine_name, exc)

    # Phase 3 — read-decide-write in one boundary. Rows are read *after* the
    # I/O, so a terminate that landed during phase 2 is already visible and
    # cannot be overwritten by a stale snapshot. No per-row refresh needed.
    session.rollback()  # start from a fresh transaction, discarding any stale identity map
    rows = session.exec(
        select(Instance).where(Instance.status != InstanceStatus.TERMINATED)
    ).all()

    # Rows this process touched at any point since the listing began. Their
    # entry in `listings` predates an operation that has since changed the VM,
    # so applying it would undo work that already succeeded — a stop reverted
    # to Running one second after `stop` returned, which is how this was found.
    busy_now, operations_now = _operation_state()
    stale = (
        busy_at_start
        | busy_now
        | {
            key
            for key, count in operations_now.items()
            if count != operations_at_start.get(key, 0)
        }
    )

    updated = 0
    # Accumulated, not written inline: an event must never be recorded for a
    # change that a later rollback undoes, so nothing is emitted until the one
    # commit below has actually landed.
    corrections: list[tuple[str, str, tuple, tuple]] = []
    for instance in rows:
        actual = listings.get(instance.engine)
        if actual is None:
            # Either the engine is retired (no driver) or its listing failed
            # this pass. Both mean we have no evidence, so change nothing.
            continue
        if instance.id in stale:
            logger.debug(
                "Reconcile: skipping '%s' — an operation ran during this pass",
                instance.name,
            )
            continue

        before = _row_snapshot(instance)
        info = actual.get(
            instance.name,
            InstanceInfo(name=instance.name, exists=False, status=None, ip_address=None),
        )
        _apply_info(instance, info)
        after = _row_snapshot(instance)
        if before != after:
            session.add(instance)
            corrections.append((instance.id, instance.name, before, after))
            updated += 1
    if updated:
        session.commit()

    # The event nobody could see before this existed. A VM stopped from outside
    # the API, a hypervisor that dropped a guest, an address that appeared late:
    # all of them used to alter the record silently, leaving a row whose history
    # said only "updated_at moved".
    for instance_id, name, before, after in corrections:
        summary, detail = _describe_correction(before, after)
        record_event(
            EventKind.RECONCILED,
            summary,
            instance_id=instance_id,
            instance_name=name,
            actor=EventActor.RECONCILER,
            detail=detail,
        )

    logger.info("Reconciliation complete: %d row(s) updated", updated)
    return updated


# --------------------------------------------------------------------------- #
# Background provisioning job
# --------------------------------------------------------------------------- #
def _fail_provisioning(session: Session, instance: Instance, message: str) -> None:
    """Mark a launch failed and record why. The five failure paths' shared tail.

    One function rather than five copies of the same six lines, and — more to
    the point — one place where the event is emitted, so a new failure mode
    cannot be added that forgets to record itself.
    """
    instance.status = InstanceStatus.ERROR
    instance.error_message = message
    _touch(instance)
    session.add(instance)
    session.commit()
    record_event(
        EventKind.PROVISIONING_FAILED,
        "Provisioning failed",
        instance=instance,
        detail=message,
    )


def _provision_job(instance_id: str) -> None:
    """Background task: Pending -> Provisioning -> Running (or Error).

    Runs after the HTTP response has been sent, so it owns its own DB session
    (the request-scoped one is already closed) and never raises past here — a
    failed launch is recorded on the row, not lost.
    """
    registry = get_engine_registry()
    settings = get_settings()

    with Session(db_engine) as session, _api_operation(instance_id):
        instance = session.get(Instance, instance_id)
        if instance is None:  # deleted before the job ran
            logger.warning("Provision job: instance %s vanished before launch", instance_id)
            return

        try:
            compute = registry.get(instance.engine)
        except UnknownEngineError as exc:
            logger.error("Provision job: %s", exc)
            _fail_provisioning(session, instance, str(exc))
            return

        instance.status = InstanceStatus.PROVISIONING
        _touch(instance)
        session.add(instance)
        session.commit()
        record_event(
            EventKind.PROVISIONING_STARTED,
            f"Provisioning started on {instance.engine}",
            instance=instance,
        )

        try:
            options = _build_launch_options(instance, session, settings)
        except ValueError as exc:
            logger.error("Launch options for '%s' invalid: %s", instance.name, exc)
            _fail_provisioning(session, instance, str(exc))
            return

        # Build the per-instance cloud-init (default user + the selected SSH
        # keys + baseline packages). If this fails we must NOT silently launch a
        # VM with no way in — record the error and stop.
        #
        # Skipped for guests that won't read it: an ISO installer, or an
        # imported image with no cloud-init. Those are console-access guests,
        # and generating a key they'll never trust would only imply otherwise.
        cloud_init_path = None
        if options.seed_cloud_init:
            try:
                cloud_init_path = build_cloud_init(
                    instance.name,
                    settings,
                    public_keys=instance_public_keys(session, instance),
                    custom_user_data=instance.user_data,
                )
            except NoUsableKeysError as exc:
                # Every selected key was deleted before we got here. Booting a
                # guest that reports SSH access it cannot honour is worse than
                # not booting it.
                logger.error("Keys for '%s' unavailable: %s", instance.name, exc)
                _fail_provisioning(session, instance, str(exc))
                return
            except CloudInitError as exc:
                logger.error("Cloud-init generation for '%s' failed: %s", instance.name, exc)
                _fail_provisioning(
                    session, instance, f"Cloud-init generation failed: {exc}"
                )
                return

        try:
            compute.provision_instance(
                name=instance.name,
                # The row is authoritative: presets only ever filled these in.
                cpus=instance.cpus or 1,
                memory=str(instance.memory_mb or 1024),
                disk=f"{instance.disk_gb or 5}G",
                cloud_init_path=str(cloud_init_path) if cloud_init_path else None,
                options=options,
            )
        except ComputeEngineError as exc:
            logger.error("Provisioning '%s' failed: %s", instance.name, exc)
            _fail_provisioning(session, instance, str(exc))
            return
        finally:
            # The cloud-init file has served its purpose once launch returns,
            # whether it succeeded or failed.
            cleanup_cloud_init(cloud_init_path)

        # Launch succeeded — pull state from the hypervisor. ISO guests have no
        # address to wait for, so don't spend the IP budget on them.
        _settle_after_launch(
            session, compute, instance, settings, expect_ip=options.seed_cloud_init
        )

        # The launch call returned without raising, so the VM exists. Whether it
        # published an address is a separate question — `degraded` answers that,
        # and the summary says which of the two happened rather than claiming a
        # reachable guest we have no evidence for.
        where = f" at {instance.ip_address}:{instance.ssh_port}" if instance.ip_address else ""
        record_event(
            EventKind.PROVISIONING_SUCCEEDED,
            f"Provisioned{where}",
            instance=instance,
            detail=(
                None if instance.ip_address
                else "The VM launched but published no address within the settle window."
            ),
        )


def _clone_job(source_id: str, clone_id: str) -> None:
    """Background task: flatten the source disk, seed it afresh, boot it.

    Deliberately not a call to ``_provision_job``: that builds a disk from a
    backing image, and the whole point here is that the disk comes from another
    instance. Everything after the disk — the seed, the boot, the settle — is
    the same work, so it is done the same way.
    """
    registry = get_engine_registry()
    settings = get_settings()

    with Session(db_engine) as session, _api_operation(clone_id):
        clone = session.get(Instance, clone_id)
        source = session.get(Instance, source_id)
        if clone is None:
            return
        if source is None:
            _fail_provisioning(session, clone, "The source instance no longer exists")
            return

        try:
            compute = registry.get(clone.engine)
        except UnknownEngineError as exc:
            _fail_provisioning(session, clone, str(exc))
            return

        clone.status = InstanceStatus.PROVISIONING
        _touch(clone)
        session.add(clone)
        session.commit()
        record_event(
            EventKind.PROVISIONING_STARTED,
            f"Copying {source.name}'s disk",
            instance=clone,
        )

        try:
            compute.clone_disk(source.name, clone.name, clone.disk_gb)
        except ComputeEngineError as exc:
            logger.error("Cloning '%s' failed: %s", source.name, exc)
            _fail_provisioning(session, clone, str(exc))
            return

        # A fresh seed, so the guest gets its own instance-id and hostname
        # rather than believing it is the machine it was copied from.
        cloud_init_path = None
        if clone.boot_source is BootSource.IMAGE and clone.ssh_enabled is not False:
            try:
                cloud_init_path = build_cloud_init(
                    clone.name,
                    settings,
                    public_keys=instance_public_keys(session, clone),
                    custom_user_data=clone.user_data,
                )
            except (NoUsableKeysError, CloudInitError) as exc:
                logger.warning("Clone '%s' seed generation failed: %s", clone.name, exc)

        try:
            compute.boot_cloned_instance(
                name=clone.name,
                cpus=clone.cpus or 1,
                memory=str(clone.memory_mb or 1024),
                cloud_init_path=str(cloud_init_path) if cloud_init_path else None,
                options=LaunchOptions(accel=clone.accel, display=clone.display),
            )
        except ComputeEngineError as exc:
            logger.error("Booting clone '%s' failed: %s", clone.name, exc)
            _fail_provisioning(session, clone, str(exc))
            return
        finally:
            cleanup_cloud_init(cloud_init_path)

        _settle_after_launch(session, compute, clone, settings, expect_ip=True)
        record_event(
            EventKind.PROVISIONING_SUCCEEDED,
            f"Clone of {source.name} ready",
            instance=clone,
        )


def _build_launch_options(
    instance: Instance, session: Session, settings: Settings
) -> LaunchOptions:
    """Turn a row's boot choices into driver-level launch options.

    Raises ``ValueError`` with a user-facing message when a choice can't be
    honoured — a missing ISO or image is the operator's mistake, and the row
    should say so rather than failing deep inside the engine.
    """
    if instance.boot_source is BootSource.ISO:
        if not instance.iso:
            raise ValueError("ISO boot was requested but no ISO was named")
        try:
            iso_path = resolve_iso(instance.iso, settings)
        except IsoError as exc:
            raise ValueError(str(exc)) from exc
        return LaunchOptions(
            accel=instance.accel,
            display=instance.display,
            iso_path=str(iso_path),
            # A generic installer knows nothing about NoCloud; a second CD-ROM
            # would only muddy the boot order.
            seed_cloud_init=False,
        )

    backing_image: str | None = None
    seed = True
    if instance.image_id:
        image = session.get(Image, instance.image_id)
        if image is None:
            raise ValueError(f"Image '{instance.image_id}' no longer exists")
        if not _image_is_launchable(image):
            raise ValueError(
                f"Image '{image.name}' is {image.status.value}, not Available"
            )
        if image.source is not ImageSource.BUILTIN:
            backing_image = str(image_path(image, settings))
        # An image without cloud-init cannot consume our key. Treat it like an
        # ISO guest — console-first — instead of implying SSH will work.
        seed = image.has_cloud_init

    return LaunchOptions(
        accel=instance.accel,
        display=instance.display,
        backing_image=backing_image,
        seed_cloud_init=seed,
    )


def _settle_after_launch(
    session: Session,
    compute: ComputeEngine,
    instance: Instance,
    settings: Settings,
    *,
    expect_ip: bool = True,
) -> None:
    """Fold the hypervisor's view into the row, waiting for an address.

    Sampling once is not enough. A hypervisor routinely reports a VM as Running
    a beat before it publishes the guest's IP, and a busy or wedged one can fail
    the call outright — in either case a single sample writes "Running with no
    IP", which is then what the dashboard shows *forever*, because the only
    other reconcile is the one behind the Refresh button.

    So: keep sampling until an IP lands or the budget runs out. For QEMU this
    returns on the first pass (provisioning already waited for SSH, and the
    address is the fixed loopback forward), so it costs nothing there.
    """
    deadline = time.monotonic() + settings.post_launch_ip_timeout_seconds
    last_error: ComputeEngineError | None = None

    while True:
        try:
            # The launch call returned, so this caller is the authority on
            # whether provisioning finished.
            _sync_instance(session, compute, instance, authoritative=True)
            last_error = None
            # Guests with no injected key never publish an address; waiting for
            # one would just burn the whole budget before reporting success.
            if instance.ip_address or not expect_ip:
                return
        except ComputeEngineError as exc:
            # get_instance_info raises before touching the session, so the row
            # is untouched and safe to retry.
            last_error = exc

        if time.monotonic() >= deadline:
            break
        time.sleep(settings.post_launch_poll_seconds)

    if last_error is not None:
        # The VM did launch; we just can't see it. Record Running so the row
        # isn't stuck Provisioning — the background reconciler will correct
        # both status and IP once the hypervisor answers again.
        logger.warning(
            "Post-launch reconcile of '%s' failed: %s", instance.name, last_error
        )
        if instance.status in (InstanceStatus.PENDING, InstanceStatus.PROVISIONING):
            instance.status = InstanceStatus.RUNNING
            _touch(instance)
            session.add(instance)
            session.commit()
    else:
        logger.warning(
            "'%s' reported no IP within %ss of launch; leaving it to the reconciler",
            instance.name,
            settings.post_launch_ip_timeout_seconds,
        )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.post(
    "",
    response_model=InstanceRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Provision a new instance (async)",
)
def create_instance(
    payload: InstanceCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    """Accept a launch request and provision it in the background (202)."""
    sizing = _resolve_sizing(payload, settings)

    # Unknown engine names are rejected by InstanceCreate's validator (422); this
    # only fails if the catalog and the driver table disagree.
    try:
        compute = registry.get(payload.engine)
    except UnknownEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # The ISO must exist before we accept the request — discovering it in the
    # background job would mean a 202 followed by a mysterious Error row.
    # Refused here rather than modelled as unsupported further down, so the
    # message names what is missing instead of failing deep in the engine with
    # "no bootable device". See GuestOS for the full list.
    if payload.guest_os not in PROVISIONABLE_GUEST_OS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Cannot provision a {payload.guest_os.value} guest yet. The boot "
                f"disk is attached over virtio-blk and the NIC is virtio-net, "
                f"neither of which Windows Setup has a driver for, and the "
                f"generated cloud-config is Linux-shaped. Windows needs a "
                f"SATA boot disk, an e1000e NIC or the virtio-win driver ISO, "
                f"and Cloudbase-Init — none of which this build has."
            ),
        )

    boot_source = BootSource.IMAGE
    if payload.iso:
        try:
            resolve_iso(payload.iso, settings)
        except IsoError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        boot_source = BootSource.ISO

    # Resolve the backing image up front for the same reason: a bad choice
    # should be a 422 on the request, not an Error row seconds later. An ISO
    # instance installs onto a blank disk, so it has no backing image at all.
    image_id = None
    image = None
    if boot_source is BootSource.IMAGE:
        image = _resolve_launch_image(session, payload.image_id)
        if image is not None:
            image_id = image.id

    # Names are unique among *live* instances only. Terminated rows are kept as
    # an audit trail and must not reserve their name forever — the hypervisor
    # has no VM by that name any more, so nothing can collide.
    existing = session.exec(
        select(Instance)
        .where(Instance.name == payload.name)
        .where(Instance.status != InstanceStatus.TERMINATED)
    ).first()
    if existing is not None:
        # Names are global, not per-project (DECISIONS #20): the name is the
        # hypervisor's identity — the on-disk directory, the QEMU process
        # label, the cloud-init instance-id and the guest hostname — so two
        # projects each holding a 'web' would share one disk.
        #
        # The message names the project holding it, which is only safe to do
        # because a project is not a security boundary. If it ever became one,
        # this line would be an information leak and would have to change.
        where = ""
        if existing.project_id:
            owner = session.get(Project, existing.project_id)
            if owner is not None:
                where = f" in project '{owner.name}'"
        raise HTTPException(
            status_code=409,
            detail=(
                f"An instance named '{payload.name}' already exists{where} "
                f"(state: {existing.status.value}). Instance names are unique "
                f"across all projects."
            ),
        )

    if not compute.is_available():
        raise HTTPException(
            status_code=503,
            detail=f"Hypervisor for engine '{payload.engine}' is unavailable",
        )

    _validate_sizing(
        sizing,
        get_capacity(session, settings),
        settings,
        minimum_disk_gb=_image_minimum_disk_gb(image) if boot_source is BootSource.IMAGE else None,
    )

    # Resolved before the row is written so an unknown key id is a 422 on the
    # request, not an Error row seconds later — the same reasoning as the ISO
    # and image checks above.
    keypairs = _resolve_keypairs(session, payload.keypair_ids)
    _validate_user_data(payload.user_data, boot_source)

    instance = Instance(
        name=payload.name,
        flavor=sizing.label,
        cpus=sizing.cpus,
        memory_mb=sizing.memory_mb,
        disk_gb=sizing.disk_gb,
        engine=payload.engine,
        # Store only a *decided* accelerator. "auto" is a request, not a fact,
        # and the reconciler overwrites this with what the VM actually got.
        accel=payload.accel if payload.accel != DEFAULT_ACCEL else None,
        display=payload.display,
        boot_source=boot_source,
        iso=payload.iso,
        image_id=image_id,
        user_data=payload.user_data or None,
        guest_os=payload.guest_os,
        project_id=resolve_project_id(session, payload.project_id),
        network_id=_resolve_network_id(session, payload.network_id),
    )
    session.add(instance)
    session.commit()
    session.refresh(instance)

    _record_keypairs(session, instance, keypairs)
    session.commit()

    invalidate_cache()
    if boot_source is BootSource.ISO:
        source = f"ISO {instance.iso}"
    else:
        source = image.name if image is not None else "the built-in image"
    record_event(
        EventKind.CREATED,
        f"Created from {source}",
        instance=instance,
        detail=(
            f"{sizing.cpus} vCPU / {sizing.memory_mb} MB / {sizing.disk_gb} GB, "
            f"preset {instance.flavor}, engine {instance.engine}, "
            f"{len(keypairs)} key pair(s)"
        ),
    )
    background_tasks.add_task(_provision_job, instance.id)
    logger.info(
        "Accepted instance '%s' (%s: %d vCPU / %d MB / %d GB, engine=%s) -> %s",
        instance.name, instance.flavor, sizing.cpus, sizing.memory_mb,
        sizing.disk_gb, instance.engine, instance.id,
    )
    return instance


@router.get("", response_model=list[InstanceRead], summary="List instances")
def list_instances(
    include_terminated: bool = Query(
        False, description="Include Terminated instances in the result"
    ),
    project_id: str | None = Depends(project_filter),
    session: Session = Depends(get_session),
) -> list[Instance]:
    stmt = select(Instance)
    if not include_terminated:
        stmt = stmt.where(Instance.status != InstanceStatus.TERMINATED)
    if project_id is not None:
        stmt = stmt.where(Instance.project_id == project_id)
    return list(session.exec(stmt.order_by(Instance.created_at)).all())


@router.get("/{instance_id}", response_model=InstanceRead, summary="Get one instance")
def get_instance(
    instance_id: str,
    session: Session = Depends(get_session),
) -> Instance:
    return _get_or_404(session, instance_id)


@router.get(
    "/{instance_id}/keypairs",
    response_model=list[InstanceKeyPairRead],
    summary="Key pairs installed on this instance",
)
def list_instance_keypairs(
    instance_id: str,
    session: Session = Depends(get_session),
) -> list[InstanceKeyPairRead]:
    """What is in this guest's authorized_keys, as recorded at launch.

    ``deleted`` marks a key whose catalog row is gone. The key itself is still
    in the guest — removing the record never reached into the VM — so the
    detail view says so rather than dropping the row and implying the access
    went with it.
    """
    _get_or_404(session, instance_id)
    links = session.exec(
        select(InstanceKeyPair).where(InstanceKeyPair.instance_id == instance_id)
    ).all()
    return [
        InstanceKeyPairRead(
            keypair_id=link.keypair_id,
            name=link.keypair_name,
            fingerprint=link.fingerprint,
            deleted=session.get(KeyPair, link.keypair_id) is None,
        )
        for link in links
    ]


@router.post(
    "/{instance_id}/start",
    response_model=InstanceRead,
    summary="Start a stopped instance",
)
def start_instance(
    instance_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    instance = _get_or_404(session, instance_id)
    if instance.status != InstanceStatus.STOPPED:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot start an instance in state '{instance.status.value}' "
            "(only Stopped instances can be started)",
        )
    compute = _engine_for(registry, instance)
    # Claimed for the whole operation, not just the driver call: the sync that
    # follows is part of the same change, and a reconcile pass landing between
    # them would report it as unexplained. See _api_operation.
    with _api_operation(instance.id):
        try:
            compute.start_instance(instance.name)
        except HypervisorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ComputeEngineError as exc:
            _mark_error(session, instance, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        _sync_instance(session, compute, instance)
    record_event(EventKind.STARTED, "Started", instance=instance)
    return instance


@router.post(
    "/{instance_id}/restart",
    response_model=InstanceRead,
    summary="Restart a running instance",
)
def restart_instance(
    instance_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    """Graceful restart: ACPI powerdown, wait for exit, boot again.

    Deliberately not QMP ``system_reset``, which is the reset button — it takes
    the power away without telling the guest. This is the stop path followed by
    the start path, so it inherits the 90-second grace period and the forced
    kill backstop, and a guest with dirty pages gets to flush them.

    Ports are pinned for an instance's life, so the SSH command a user has
    already copied still works afterwards.
    """
    instance = _get_or_404(session, instance_id)
    if instance.status != InstanceStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot restart an instance in state '{instance.status.value}' "
            "(only Running instances can be restarted)",
        )
    compute = _engine_for(registry, instance)

    with _api_operation(instance.id):
        try:
            compute.restart_instance(instance.name)
        except HypervisorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ComputeEngineError as exc:
            _mark_error(session, instance, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        _sync_instance(session, compute, instance)
    record_event(EventKind.RESTARTED, "Restarted", instance=instance)
    return instance


@router.post(
    "/{instance_id}/clone",
    response_model=InstanceRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Clone a stopped instance (async)",
)
def clone_instance(
    instance_id: str,
    payload: InstanceClone,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    """Copy a stopped instance's disk into a new instance.

    The copy is **flattened** (``qemu-img convert``), not an overlay on the
    source. It costs the full allocated size and takes as long as the data
    takes, and in exchange the clone owns its bytes: terminating the source
    later cannot corrupt it. See DECISIONS.

    Three things the clone deliberately does *not* inherit:

    * **Ports.** Fresh SSH/VNC/QMP allocations, or two instances would fight
      over one host port.
    * **cloud-init identity.** A new NoCloud seed with the clone's own
      ``instance-id`` and hostname, so the guest does not come up believing it
      is the machine it was copied from.
    * **Snapshots and volumes.** Snapshots live inside the source's overlay and
      are flattened away; volumes belong to the source and stay attached to it.

    What it *does* inherit, and what the confirm dialog must say: the guest's
    **SSH host keys**. Two machines presenting the same host identity will trip
    a client's known_hosts check, and regenerating them means reaching into the
    guest — which this system does not do.
    """
    source = _get_or_404(session, instance_id)
    compute = _engine_for(registry, source)
    if not compute.supports_clone:
        raise HTTPException(
            status_code=409,
            detail=f"The '{source.engine}' engine cannot clone instances",
        )
    if source.status is not InstanceStatus.STOPPED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot clone '{source.name}' while it is {source.status.value}. "
                f"Copying a disk underneath a running guest captures a filesystem "
                f"mid-write, and this hypervisor does not lock it on every "
                f"platform. Stop the instance first."
            ),
        )

    existing = session.exec(
        select(Instance)
        .where(Instance.name == payload.name)
        .where(Instance.status != InstanceStatus.TERMINATED)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"An instance named '{payload.name}' already exists "
            f"(state: {existing.status.value})",
        )

    # A clone is a full-size copy, so it is checked against free disk exactly
    # as a new instance is rather than assumed to fit because the original did.
    _validate_sizing(
        ResolvedSizing(
            label=source.flavor,
            cpus=source.cpus or 1,
            memory_mb=source.memory_mb or 1024,
            disk_gb=source.disk_gb or 5,
        ),
        get_capacity(session, settings),
        settings,
    )

    clone = Instance(
        name=payload.name,
        flavor=source.flavor,
        cpus=source.cpus,
        memory_mb=source.memory_mb,
        disk_gb=source.disk_gb,
        engine=source.engine,
        accel=source.accel,
        display=source.display,
        boot_source=source.boot_source,
        iso=source.iso,
        image_id=source.image_id,
        user_data=source.user_data,
        guest_os=source.guest_os,
        project_id=source.project_id,
        network_id=source.network_id,
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)

    # The same keys the source was launched with, so the clone is reachable by
    # whoever could reach the original.
    _record_keypairs(
        session,
        clone,
        [
            keypair
            for keypair in (
                session.get(KeyPair, link.keypair_id)
                for link in session.exec(
                    select(InstanceKeyPair).where(
                        InstanceKeyPair.instance_id == source.id
                    )
                ).all()
            )
            if keypair is not None
        ],
    )
    session.commit()

    invalidate_cache()
    record_event(
        EventKind.CLONED,
        f"Cloned from '{source.name}'",
        instance=clone,
        detail=(
            f"Flattened copy of {source.name}'s disk — the clone owns its data "
            f"and survives the original being terminated. Its SSH host keys are "
            f"identical to the original's."
        ),
    )
    background_tasks.add_task(_clone_job, source.id, clone.id)
    logger.info("Accepted clone of '%s' -> '%s'", source.name, clone.name)
    return clone


@router.post(
    "/{instance_id}/stop",
    response_model=InstanceRead,
    summary="Stop a running instance",
)
def stop_instance(
    instance_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    instance = _get_or_404(session, instance_id)
    if instance.status != InstanceStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot stop an instance in state '{instance.status.value}' "
            "(only Running instances can be stopped)",
        )
    compute = _engine_for(registry, instance)
    with _api_operation(instance.id):
        try:
            compute.stop_instance(instance.name)
        except HypervisorUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ComputeEngineError as exc:
            _mark_error(session, instance, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        _sync_instance(session, compute, instance)
    record_event(EventKind.STOPPED, "Stopped", instance=instance)
    return instance


@router.delete(
    "/{instance_id}",
    response_model=InstanceRead,
    summary="Terminate an instance (delete + purge)",
)
def delete_instance(
    instance_id: str,
    force: bool = Query(
        False,
        description=(
            "Clear the row even if the hypervisor refuses to destroy the VM. "
            "Leaves any surviving VM behind — see the route docstring."
        ),
    ),
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Instance:
    """Delete + purge on the hypervisor and mark the row Terminated.

    Allowed from any state; the audit row is retained rather than deleted.

    ``force`` is the escape hatch for a wedged hypervisor. Normally a driver
    failure is a 502 and the row is marked ``Error`` — the right default,
    because a row that says Terminated while the VM is still running is a lie,
    and the usual fix is to retry once the hypervisor recovers. But a VM whose
    files were deleted underneath it (or a hypervisor that never comes back)
    would otherwise leave a row nothing can clear. ``force`` still *attempts*
    the destroy and only ignores its failure, so the fast path is unchanged; it
    is opt-in because the leak it can cause has to be the caller's choice.
    """
    instance = _get_or_404(session, instance_id)
    if instance.status == InstanceStatus.TERMINATED:
        # Idempotent — and load-bearing now that names are reusable: another
        # instance may already own this name, and destroying by name here would
        # tear down *its* VM.
        return instance

    forced_reason: str | None = None
    # Held from before the destroy until the row says Terminated. Destroying a
    # VM makes it vanish from the hypervisor, and a reconcile pass landing in
    # that window would record "Corrected to Error: VM no longer exists" — an
    # alarming line describing a delete that worked exactly as asked.
    with _api_operation(instance.id):
        if registry.supports(instance.engine):
            compute = registry.get(instance.engine)
            try:
                compute.destroy_instance(instance.name)
            except (HypervisorUnavailableError, ComputeEngineError) as exc:
                if not force:
                    if isinstance(exc, HypervisorUnavailableError):
                        raise HTTPException(status_code=503, detail=str(exc)) from exc
                    _mark_error(session, instance, str(exc))
                    raise HTTPException(status_code=502, detail=str(exc)) from exc
                logger.warning(
                    "Force-terminating '%s' after a destroy failure: %s", instance.name, exc
                )
                forced_reason = (
                    f"The hypervisor refused to destroy the VM and --force was used, so "
                    f"the record was cleared anyway. Any surviving VM was left behind.\n\n{exc}"
                )
        else:
            # A row from a retired engine. There is no driver to purge the VM with —
            # it went away with its hypervisor — but the user must still be able to
            # clear the row, so terminate is allowed to proceed on the record alone.
            logger.info(
                "Force-terminating '%s': no driver for retired engine '%s'",
                instance.name, instance.engine,
            )
            forced_reason = (
                f"No driver exists for the retired '{instance.engine}' engine, so the "
                "record was cleared without a hypervisor call."
            )

        # Snapshots live inside the overlay that is about to be deleted, so their
        # rows go with it. Keeping them would advertise restore points whose data
        # was destroyed moments earlier.
        from app.routers.snapshots import delete_snapshots_for_instance

        dropped_snapshots = delete_snapshots_for_instance(session, instance.id)

        # Volumes are the opposite case, and the distinction is the whole point
        # of the feature: a snapshot lives *inside* this instance's overlay, but
        # a volume is a separate file that was merely attached to it — often
        # holding the only copy of something. Terminating an instance detaches
        # them and leaves them Available with their data intact. Destroying a
        # volume because someone destroyed the VM it happened to be plugged into
        # is the one unforgivable bug in this part of the system.
        from app.routers.volumes import detach_volumes_for_instance

        detached_volumes = detach_volumes_for_instance(session, instance.id)

        # The VM's SLIRP stack is gone, so its host ports are already released;
        # the rows would otherwise advertise forwards into nothing.
        from app.routers.networks import delete_forwards_for_instance

        delete_forwards_for_instance(session, instance.id)

        instance.status = InstanceStatus.TERMINATED
        instance.ip_address = None
        instance.error_message = None
        # The VM and its host port forwards are gone; keeping the numbers would
        # advertise an SSH endpoint that no longer exists.
        instance.ssh_port = None
        instance.vnc_port = None
        instance.qmp_port = None
        instance.pid = None
        _touch(instance)
        session.add(instance)
        session.commit()
        session.refresh(instance)

    # Written last, and this is the event most likely to be read: it is the only
    # record that the instance ever existed once someone stops looking at the
    # Terminated row.
    detail_parts = [forced_reason] if forced_reason else []
    if dropped_snapshots:
        detail_parts.append(
            f"{dropped_snapshots} snapshot(s) were destroyed with the disk."
        )
    if detached_volumes:
        detail_parts.append(
            f"{len(detached_volumes)} volume(s) were detached and kept: "
            f"{', '.join(sorted(detached_volumes))}."
        )
    record_event(
        EventKind.FORCE_TERMINATED if forced_reason else EventKind.TERMINATED,
        "Force-terminated" if forced_reason else "Terminated",
        instance=instance,
        detail="\n\n".join(detail_parts) or None,
    )
    logger.info("Terminated instance '%s'", instance.name)
    return instance


@router.websocket("/{instance_id}/console")
async def instance_console(websocket: WebSocket, instance_id: str) -> None:
    """Bridge the browser to this VM's VNC framebuffer.

    Accepts first and *then* reports refusals as close codes: a WebSocket
    rejected before the handshake gives the browser a bare "connection failed"
    with nothing to show the user, whereas a code + reason after accept can be
    rendered verbatim in the console panel.

    The DB row is read into locals and the session released immediately — a
    console can stay open for hours and must not hold a pooled connection.
    """
    await websocket.accept()

    with Session(db_engine) as session:
        instance = session.get(Instance, instance_id)
        if instance is None:
            await _close_console(websocket, CLOSE_NOT_FOUND, f"No instance '{instance_id}'")
            return
        name, engine_name = instance.name, instance.engine
        status_value, vnc_port = instance.status, instance.vnc_port

    if engine_name != "qemu":
        await _close_console(
            websocket,
            CLOSE_CONFLICT,
            f"The console is only available for QEMU instances (this one runs on {engine_name})",
        )
        return
    if status_value != InstanceStatus.RUNNING:
        await _close_console(
            websocket,
            CLOSE_CONFLICT,
            f"'{name}' is {status_value.value} — start it to open the console",
        )
        return
    if not vnc_port:
        await _close_console(
            websocket, CLOSE_CONFLICT, f"'{name}' has no VNC port recorded"
        )
        return

    await bridge_websocket_to_vnc(websocket, HOST_IP, vnc_port, label=name)


async def _close_console(websocket: WebSocket, code: int, reason: str) -> None:
    logger.info("Console refused (%d): %s", code, reason)
    await websocket.close(code=code, reason=reason)


@router.post("/refresh", response_model=list[InstanceRead], summary="Reconcile with hypervisor")
def refresh_instances(
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> list[Instance]:
    """Manually trigger reconciliation, then return the live (non-terminated) set."""
    try:
        reconcile_all(session, registry)
    except HypervisorUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ComputeEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return list(
        session.exec(
            select(Instance)
            .where(Instance.status != InstanceStatus.TERMINATED)
            .order_by(Instance.created_at)
        ).all()
    )


# --------------------------------------------------------------------------- #
def _mark_error(session: Session, instance: Instance, message: str) -> None:
    instance.status = InstanceStatus.ERROR
    instance.error_message = message
    _touch(instance)
    session.add(instance)
    session.commit()
    session.refresh(instance)
    # The message is the whole value here. `error_message` on the row holds only
    # the most recent one, so a second failure used to erase the first — and the
    # first is usually the one that explains the second.
    record_event(EventKind.ERRORED, "Entered Error", instance=instance, detail=message)
