"""
SSH key pair catalog.

Before this, every instance trusted exactly one key: the orchestrator's,
generated on first run and hardcoded into every cloud-init document. That is
fine until you want to reach a VM from a second machine, or hand one to a
colleague, or use the key you already have.

The orchestrator key is not replaced by any of this — it is *adopted* as a row
so it appears alongside the others, stays the default, and cannot be deleted.
Every instance launched in the last nine phases has it baked into its
authorized_keys, and a catalog that let you delete it would be offering to
break them.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from app.config import Settings, get_settings
from app.database import engine as db_engine
from app.database import get_session
from app.routers.projects import project_filter, resolve_project_id
from app.keypairs import KeyPairError, generate_keypair, parse_public_key
from app.models import (
    KeyPair,
    KeyPairGenerateRequest,
    KeyPairImportRequest,
    KeyPairRead,
    KeyPairSource,
)

logger = logging.getLogger("kurukuru.keypairs")

router = APIRouter(prefix="/keypairs", tags=["keypairs"])

#: Display name of the keypair the orchestrator generates for itself.
ORCHESTRATOR_KEY_NAME = "orchestrator"


def _get_or_404(session: Session, keypair_id: str) -> KeyPair:
    keypair = session.get(KeyPair, keypair_id)
    if keypair is None:
        raise HTTPException(status_code=404, detail=f"Key pair '{keypair_id}' not found")
    return keypair


def ensure_orchestrator_keypair(settings: Settings | None = None) -> None:
    """Adopt the orchestrator's existing keypair as a catalog row.

    Idempotent, and deliberately *adoption* rather than creation: the key is
    read through ``ssh_keys`` exactly as before, so the file on disk is neither
    moved nor regenerated. Instances already in the field trust it.

    Runs at startup like the built-in image registration, and tolerates
    failure the same way — a machine with no ssh-keygen should still serve the
    rest of the API rather than refusing to boot.
    """
    settings = settings or get_settings()
    from app.ssh_keys import SSHKeyError, get_private_key_path, get_public_key

    try:
        public_key = get_public_key(settings)
        private_path = str(get_private_key_path(settings))
    except SSHKeyError as exc:
        logger.warning("Could not adopt the orchestrator keypair: %s", exc)
        return

    try:
        parsed = parse_public_key(public_key)
    except KeyPairError as exc:  # pragma: no cover - our own key is well-formed
        logger.warning("Orchestrator public key did not parse: %s", exc)
        return

    with Session(db_engine) as session:
        row = session.exec(
            select(KeyPair).where(KeyPair.source == KeyPairSource.ORCHESTRATOR)
        ).first()
        if row is None:
            row = KeyPair(name=ORCHESTRATOR_KEY_NAME, source=KeyPairSource.ORCHESTRATOR)
        # Re-synced every start: the key file is the truth, and if an operator
        # ever replaces it the catalog should follow rather than describe a key
        # that no longer exists.
        row.public_key = parsed.normalised
        row.fingerprint = parsed.fingerprint
        row.key_type = parsed.label
        row.has_private_key = True
        row.private_key_path = private_path
        session.add(row)
        session.commit()
        logger.info("Orchestrator keypair registered (%s)", parsed.fingerprint)


def orchestrator_keypair(session: Session) -> KeyPair | None:
    """The default keypair for a launch that doesn't name any."""
    return session.exec(
        select(KeyPair).where(KeyPair.source == KeyPairSource.ORCHESTRATOR)
    ).first()


@router.get("", response_model=list[KeyPairRead], summary="List key pairs")
def list_keypairs(
    project_id: str | None = Depends(project_filter),
    session: Session = Depends(get_session),
) -> list[KeyPair]:
    stmt = select(KeyPair)
    if project_id is not None:
        # The orchestrator key is exempt, and it is the one exemption in the
        # codebase. It is the default for every launch in every project, so a
        # filtered view that hid it would offer a launch with no key at all.
        stmt = stmt.where(
            (KeyPair.project_id == project_id)
            | (KeyPair.source == KeyPairSource.ORCHESTRATOR)
        )
    return list(session.exec(stmt.order_by(KeyPair.created_at)).all())


@router.get("/{keypair_id}", response_model=KeyPairRead, summary="Get one key pair")
def get_keypair(keypair_id: str, session: Session = Depends(get_session)) -> KeyPair:
    return _get_or_404(session, keypair_id)


@router.post(
    "/import",
    response_model=KeyPairRead,
    status_code=status.HTTP_201_CREATED,
    summary="Import an existing public key",
)
def import_keypair(
    payload: KeyPairImportRequest,
    session: Session = Depends(get_session),
) -> KeyPair:
    """Register a public key the user already has.

    Only the public half — there is no field to send a private key in, and
    nothing here would know what to do with one.
    """
    try:
        parsed = parse_public_key(payload.public_key)
    except KeyPairError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = session.exec(select(KeyPair).where(KeyPair.name == payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"A key pair named '{payload.name}' already exists"
        )

    duplicate = session.exec(
        select(KeyPair).where(KeyPair.fingerprint == parsed.fingerprint)
    ).first()
    if duplicate is not None:
        # Not an error: the same key under two names is a legitimate thing to
        # want. Worth saying, though — usually it means the user forgot.
        logger.info(
            "Imported key %s duplicates existing key pair '%s'",
            parsed.fingerprint, duplicate.name,
        )

    keypair = KeyPair(
        name=payload.name,
        public_key=parsed.normalised,
        fingerprint=parsed.fingerprint,
        key_type=parsed.label,
        source=KeyPairSource.IMPORTED,
        has_private_key=False,
        project_id=resolve_project_id(session, payload.project_id),
    )
    session.add(keypair)
    session.commit()
    session.refresh(keypair)
    logger.info("Imported key pair '%s' (%s)", keypair.name, keypair.fingerprint)
    return keypair


@router.post(
    "/generate",
    response_model=KeyPairRead,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new ed25519 key pair",
)
def generate(
    payload: KeyPairGenerateRequest,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> KeyPair:
    """Create a keypair on the backend's filesystem.

    The response carries the public key and the *path* of the private half.
    The private key is never returned by this or any other endpoint, now or on
    a later read: it is written to disk with 0600 and used by ``ssh -i``.

    Download-once semantics — hand the secret over exactly once at creation and
    never again — would need somewhere to hold it until collection and some
    notion of who is collecting it. There is no authentication in this system,
    so that decision belongs to the phase that introduces one.
    """
    existing = session.exec(select(KeyPair).where(KeyPair.name == payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"A key pair named '{payload.name}' already exists"
        )

    try:
        private_path, public_key = generate_keypair(payload.name, settings)
        parsed = parse_public_key(public_key)
    except KeyPairError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    keypair = KeyPair(
        name=payload.name,
        public_key=parsed.normalised,
        fingerprint=parsed.fingerprint,
        key_type=parsed.label,
        project_id=resolve_project_id(session, payload.project_id),
        source=KeyPairSource.GENERATED,
        has_private_key=True,
        private_key_path=str(private_path),
    )
    session.add(keypair)
    session.commit()
    session.refresh(keypair)
    logger.info("Generated key pair '%s' at %s", keypair.name, private_path)
    return keypair


@router.delete(
    "/{keypair_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a key pair",
)
def delete_keypair(
    keypair_id: str,
    session: Session = Depends(get_session),
) -> Response:
    """Remove a key pair from the catalog.

    Allowed even when instances were launched with it, and the reason matters:
    the key is already written into those guests' ``authorized_keys``. Deleting
    the row removes our *record* of it, not the access — that would need us to
    log into every affected instance and edit a file. The instance rows keep
    their association (name and fingerprint are denormalised onto it) so the
    detail view can still say what was installed.

    The private key file, if we generated one, is left on disk for the same
    reason: it may still be the only way into a running VM.
    """
    keypair = _get_or_404(session, keypair_id)

    if keypair.source is KeyPairSource.ORCHESTRATOR:
        raise HTTPException(
            status_code=409,
            detail=(
                "The orchestrator key pair cannot be deleted. Every instance "
                "launched so far trusts it, and it is the default for new ones."
            ),
        )

    session.delete(keypair)
    session.commit()
    logger.info("Deleted key pair '%s'", keypair.name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
