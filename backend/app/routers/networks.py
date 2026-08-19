"""
Networks and port forwards.

One mode ships: ``user`` — QEMU's built-in NAT, which is what every instance
has always been on. Host-only and bridged are modelled but not selectable, and
the reason is in :data:`DEFERRED_NETWORK_MODES` rather than in a disabled
button: bridged needs Administrator on Windows and root on Linux, and
host-only's portable mechanism does not work on Windows at all. Those are
useful facts about the operator's machine, so the API states them.

A guest on user-mode networking has no address of its own, so **a port forward
is the only way in**. Unlike volumes, forwards can be changed on a running
instance safely — QEMU starts listening immediately and the guest holds no
state about it — so they are applied live and replayed at launch.

The SSH forward is deliberately not a row in this table. See
:class:`app.models.PortForward` and DECISIONS #25.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.engines import (
    ComputeEngineError,
    EngineRegistry,
    UnknownEngineError,
    get_engine_registry,
)
from app.database import engine as db_engine
from app.database import get_session
from app.events import record_event
from app.models import (
    DEFERRED_NETWORK_MODES,
    FORWARD_PRESETS,
    EventKind,
    ForwardPreset,
    ForwardProtocol,
    GuestOS,
    Instance,
    InstanceStatus,
    Network,
    NetworkRead,
    PortForward,
    PortForwardCreate,
    PortForwardRead,
)
from app.routers.projects import project_filter

logger = logging.getLogger("iaas.networks")

router = APIRouter(tags=["networks"])

#: Synthetic id of the SSH forward. Not a uuid on purpose: it is not a row, and
#: a client that tried to DELETE it should get a refusal that reads as a rule
#: rather than a missing record.
SSH_FORWARD_ID = "ssh"


def default_network(session: Session) -> Network | None:
    return session.exec(select(Network).where(Network.is_default)).first()


def ensure_default_network() -> None:
    """Startup hook: guarantee the default user network exists.

    The migration seeds it too, but a database built by ``create_all`` alone
    never runs the migration — which is every test fixture.
    """
    from app.models import DEFAULT_NETWORK_NAME, NetworkMode

    with Session(db_engine) as session:
        if default_network(session) is not None:
            return
        session.add(
            Network(
                name=DEFAULT_NETWORK_NAME,
                mode=NetworkMode.USER,
                cidr="10.0.2.0/24",
                is_default=True,
            )
        )
        session.commit()
        logger.info("Created the default user network")


# --------------------------------------------------------------------------- #
# Networks
# --------------------------------------------------------------------------- #
@router.get("/networks", response_model=list[NetworkRead], summary="List networks")
def list_networks(
    project_id: str | None = Depends(project_filter),
    session: Session = Depends(get_session),
) -> list[NetworkRead]:
    """Every network. There is exactly one until another mode ships.

    Read-only: with only user mode available, a second network would be a
    second NAT that behaves identically to the first, which is a control that
    does nothing dressed as a feature.
    """
    stmt = select(Network)
    if project_id is not None:
        stmt = stmt.where(Network.project_id == project_id)
    networks = session.exec(stmt.order_by(Network.created_at)).all()

    result = []
    for network in networks:
        count = len(
            session.exec(
                select(Instance)
                .where(Instance.network_id == network.id)
                .where(Instance.status != InstanceStatus.TERMINATED)
            ).all()
        )
        result.append(NetworkRead(**network.model_dump(), instance_count=count))
    return result


@router.get("/networks/modes", summary="Network modes, including deferred ones")
def list_network_modes() -> dict[str, object]:
    """What this build can and cannot do, and what the rest would require.

    Served rather than hard-coded in the dashboard so the API, the UI and the
    docs cannot drift into disagreeing about what an operator would have to
    install. Every claim was measured — see DECISIONS #24.
    """
    return {
        "available": [
            {
                "mode": "user",
                "label": "User-mode NAT",
                "summary": (
                    "The guest gets outbound access through the host and is "
                    "reached on forwarded ports. No configuration, no elevation."
                ),
            }
        ],
        "deferred": [
            {"mode": mode, **details} for mode, details in DEFERRED_NETWORK_MODES.items()
        ],
    }


# --------------------------------------------------------------------------- #
# Port forwards
# --------------------------------------------------------------------------- #
def _instance_or_404(session: Session, instance_id: str) -> Instance:
    instance = session.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")
    return instance


def _ssh_forward(instance: Instance) -> PortForwardRead | None:
    """The SSH forward, presented as a read-only row.

    Synthesised from ``instance.ssh_port`` so the listing is the whole truth
    about what reaches this guest, while the table underneath stays free of the
    one forward that must never break.
    """
    if not instance.ssh_port:
        return None
    return PortForwardRead(
        id=SSH_FORWARD_ID,
        instance_id=instance.id,
        host_port=instance.ssh_port,
        guest_port=22,
        protocol=ForwardProtocol.TCP,
        description="SSH — created with the instance and pinned for its lifetime",
        created_at=instance.created_at,
        derived=True,
    )


def _check_collision(
    session: Session,
    settings: Settings,
    instance: Instance,
    payload: PortForwardCreate,
) -> None:
    """Refuse a host port that is already spoken for, and say by what.

    Four ways a port can be taken, and the message names which one — the same
    treatment the capacity refusals get, because "port in use" without saying
    *what* is using it sends someone to netstat for something we already know.
    """
    port = payload.host_port

    # 1. This instance's own SSH port.
    if port == instance.ssh_port:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Host port {port} is this instance's SSH port. It is pinned for "
                f"the instance's lifetime and forwards to guest port 22 already."
            ),
        )

    # 2. Any other instance's pinned ports.
    for other in session.exec(
        select(Instance).where(Instance.status != InstanceStatus.TERMINATED)
    ).all():
        if other.id == instance.id:
            continue
        for label, taken in (
            ("SSH", other.ssh_port), ("VNC", other.vnc_port), ("QMP", other.qmp_port)
        ):
            if taken == port:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Host port {port} is instance '{other.name}'s {label} port. "
                        f"Pinned ports are held for the life of the instance, so "
                        f"this one will not free up while it exists."
                    ),
                )

    # 3. A forward on this or any other instance.
    clash = session.exec(select(PortForward).where(PortForward.host_port == port)).first()
    if clash is not None:
        owner = session.get(Instance, clash.instance_id)
        where = f"instance '{owner.name}'" if owner is not None else "another instance"
        raise HTTPException(
            status_code=422,
            detail=(
                f"Host port {port} already forwards to {where} port "
                f"{clash.guest_port}. Remove that forward first, or pick "
                f"another host port."
            ),
        )

    # 4. Inside a pool this system allocates from. Not in use *yet*, but it
    #    will be handed to some future instance, and a launch failing because
    #    a forward squatted on its port is a confusing way to find out.
    pools = (
        ("SSH", settings.qemu_ssh_port_min, settings.qemu_ssh_port_max),
        ("QMP", settings.qemu_qmp_port_min, settings.qemu_qmp_port_max),
        ("VNC", settings.qemu_vnc_port_min, settings.qemu_vnc_port_max),
    )
    for label, low, high in pools:
        if low <= port <= high:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Host port {port} is inside the {label} port pool "
                    f"({low}-{high}), which this system allocates from when it "
                    f"launches instances. Forwarding it would make some future "
                    f"launch fail. Pick a port outside every pool."
                ),
            )


@router.get(
    "/instances/{instance_id}/forwards",
    response_model=list[PortForwardRead],
    summary="Port forwards reaching this instance",
)
def list_forwards(
    instance_id: str, session: Session = Depends(get_session)
) -> list[PortForwardRead]:
    """Everything that reaches this guest, SSH included.

    The SSH entry is marked ``derived`` and cannot be deleted. It is listed
    anyway because a user reading this to find out what is exposed should not
    have to know that one forward is kept somewhere else.
    """
    instance = _instance_or_404(session, instance_id)
    rows = session.exec(
        select(PortForward)
        .where(PortForward.instance_id == instance_id)
        .order_by(PortForward.created_at)
    ).all()

    forwards: list[PortForwardRead] = []
    ssh = _ssh_forward(instance)
    if ssh is not None:
        forwards.append(ssh)
    forwards.extend(PortForwardRead(**row.model_dump()) for row in rows)
    return forwards


@router.get(
    "/forwards/presets",
    response_model=list[ForwardPreset],
    summary="Named guest ports offered as one-click forwards",
)
def list_forward_presets(guest_os: GuestOS | None = None) -> list[ForwardPreset]:
    """The preset catalog, optionally narrowed to one guest family.

    A catalog rather than a create shortcut: the preset supplies the guest port
    and the explanation, and the ordinary create endpoint still does the work,
    including the host-port collision check. One code path creates a forward.
    """
    return [
        preset
        for preset in FORWARD_PRESETS
        if guest_os is None or preset.guest_os in (None, guest_os)
    ]


@router.post(
    "/instances/{instance_id}/forwards",
    response_model=PortForwardRead,
    status_code=status.HTTP_201_CREATED,
    summary="Forward a host port into this instance",
)
def create_forward(
    instance_id: str,
    payload: PortForwardCreate,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> PortForwardRead:
    """Add a forward. Applied immediately, running or not.

    This is the one thing in the system that changes a running VM's
    configuration, and it is safe for reasons that do not generalise: SLIRP
    owns the listening socket entirely, the guest is not told anything, and
    QEMU begins accepting connections on return.
    """
    instance = _instance_or_404(session, instance_id)
    if instance.status is InstanceStatus.TERMINATED:
        raise HTTPException(
            status_code=409,
            detail=f"'{instance.name}' is Terminated; there is nothing to forward to.",
        )

    _check_collision(session, settings, instance, payload)

    forward = PortForward(
        instance_id=instance_id,
        host_port=payload.host_port,
        guest_port=payload.guest_port,
        protocol=payload.protocol,
        description=payload.description,
    )

    # The engine goes first. If QEMU refuses — a port taken by something
    # outside this system since the checks above — nothing is written, so the
    # database never claims a forward that does not exist.
    try:
        compute = registry.get(instance.engine)
    except UnknownEngineError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"'{instance.name}' runs on the retired '{instance.engine}' engine.",
        ) from exc
    if not compute.supports_port_forwards:
        raise HTTPException(
            status_code=409,
            detail=f"The '{instance.engine}' engine does not support port forwards",
        )
    try:
        compute.add_port_forward(instance.name, forward.spec)
    except ComputeEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    session.add(forward)
    session.commit()
    session.refresh(forward)

    live = instance.status is InstanceStatus.RUNNING
    record_event(
        EventKind.PORT_FORWARD_ADDED,
        f"Port {forward.host_port} → guest {forward.guest_port} "
        f"({forward.protocol.value})",
        instance=instance,
        detail=(
            "Listening now."
            if live
            else "Takes effect when the instance starts."
        ),
    )
    return PortForwardRead(**forward.model_dump())


@router.delete(
    "/instances/{instance_id}/forwards/{forward_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a port forward",
)
def delete_forward(
    instance_id: str,
    forward_id: str,
    session: Session = Depends(get_session),
    registry: EngineRegistry = Depends(get_engine_registry),
) -> Response:
    instance = _instance_or_404(session, instance_id)

    if forward_id == SSH_FORWARD_ID:
        raise HTTPException(
            status_code=409,
            detail=(
                "The SSH forward cannot be removed. It is created with the "
                "instance, pinned for its lifetime, and is how 'iaas ssh' and "
                "the copied SSH command reach the guest. Terminate the instance "
                "to release the port."
            ),
        )

    forward = session.get(PortForward, forward_id)
    if forward is None or forward.instance_id != instance_id:
        raise HTTPException(status_code=404, detail=f"Forward '{forward_id}' not found")

    try:
        compute = registry.get(instance.engine)
        if compute.supports_port_forwards:
            compute.remove_port_forward(instance.name, forward.spec)
    except UnknownEngineError:
        pass  # retired engine: the row is all that is left to clean up
    except ComputeEngineError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    host_port, guest_port = forward.host_port, forward.guest_port
    session.delete(forward)
    session.commit()

    record_event(
        EventKind.PORT_FORWARD_REMOVED,
        f"Port {host_port} → guest {guest_port} removed",
        instance=instance,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def forwards_for_instance(session: Session, instance_id: str) -> list[PortForward]:
    """Stored forwards for an instance, oldest first."""
    return list(
        session.exec(
            select(PortForward)
            .where(PortForward.instance_id == instance_id)
            .order_by(PortForward.created_at)
        ).all()
    )


def delete_forwards_for_instance(session: Session, instance_id: str) -> int:
    """Drop an instance's forward rows. Called when it is terminated.

    No hypervisor work: the VM and its SLIRP stack are gone, so the host ports
    are already released. Keeping the rows would advertise forwards into a
    guest that no longer exists.
    """
    rows = forwards_for_instance(session, instance_id)
    for row in rows:
        session.delete(row)
    return len(rows)
