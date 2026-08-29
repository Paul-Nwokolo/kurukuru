"""
Disk image catalog API.

Registers user-supplied images so they can back instances exactly like the
built-in Ubuntu cloud image. Importing is asynchronous for the same reason
provisioning is: copying a multi-GB file must not hold an HTTP worker.

Deletion is guarded twice — the built-in image is not the user's to remove, and
an image still backing a live instance cannot be removed at all: every overlay
built on it would be unreadable the moment its backing file vanished, and qcow2
offers no way back from that.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlmodel import Session, select

from kurukuru.config import Settings, get_settings
from kurukuru.database import engine as db_engine
from kurukuru.database import get_session
from kurukuru.routers.projects import project_filter, resolve_project_id
from kurukuru.engines.images import base_image_path
from kurukuru.events import record_event
from kurukuru.image_store import (
    ImageError,
    fetch_into_store,
    allocate_filename,
    copy_into_store,
    delete_image_file,
    image_path,
    probe_image,
    resolve_source_file,
)
from kurukuru.models import (
    EventKind,
    Image,
    ImageFetchRequest,
    ImageImportRequest,
    ImageRead,
    ImageSource,
    ImageStatus,
    Instance,
    InstanceStatus,
)

logger = logging.getLogger("kurukuru.images")

router = APIRouter(prefix="/images", tags=["images"])

#: Display name of the image shipped with the orchestrator.
BUILTIN_IMAGE_NAME = "Ubuntu 24.04 LTS (cloud)"


def _get_or_404(session: Session, image_id: str) -> Image:
    image = session.get(Image, image_id)
    if image is None:
        raise HTTPException(status_code=404, detail=f"Image '{image_id}' not found")
    return image


def ensure_builtin_image(settings: Settings | None = None) -> None:
    """Register the shipped Ubuntu cloud image if it isn't already.

    Runs at startup so the catalog is never empty and every instance can record
    which image it came from — including ones launched without choosing.
    Idempotent, and it re-probes sizes once the file has been downloaded.
    """
    settings = settings or get_settings()
    path = base_image_path(settings)

    with Session(db_engine) as session:
        image = session.exec(
            select(Image).where(Image.source == ImageSource.BUILTIN)
        ).first()
        if image is None:
            image = Image(
                name=BUILTIN_IMAGE_NAME,
                filename=path.name,
                source=ImageSource.BUILTIN,
                has_cloud_init=True,  # it is a cloud image; that is the point
                status=ImageStatus.IMPORTING,
            )

        if not path.exists():
            # Downloaded lazily on first QEMU launch; leave it pending.
            image.status = ImageStatus.IMPORTING
            image.error_message = "Base image not downloaded yet"
        else:
            try:
                probe = probe_image(path, settings)
                image.format = probe.format
                image.virtual_size_bytes = probe.virtual_size_bytes
                image.actual_size_bytes = probe.actual_size_bytes
                image.status = ImageStatus.AVAILABLE
                image.error_message = None
            except ImageError as exc:
                image.status = ImageStatus.ERROR
                image.error_message = str(exc)

        session.add(image)
        session.commit()
        logger.info("Built-in image registered: %s (%s)", image.name, image.status.value)


def _import_job(image_id: str, source_path: str) -> None:
    """Background task: copy the file in, probe it, mark Available or Error."""
    settings = get_settings()
    with Session(db_engine) as session:
        image = session.get(Image, image_id)
        if image is None:  # deleted before the job ran
            logger.warning("Import job: image %s vanished", image_id)
            return

        try:
            source = resolve_source_file(source_path)
            # Probe before copying: a file qemu-img can't read is worth
            # rejecting in seconds rather than after a multi-GB copy.
            probe = probe_image(source, settings)
            copy_into_store(source, image.filename, settings)
            stored = probe_image(image_path(image, settings), settings)
        except ImageError as exc:
            logger.error("Import of '%s' failed: %s", image.name, exc)
            image.status = ImageStatus.ERROR
            image.error_message = str(exc)
            session.add(image)
            session.commit()
            record_event(
                EventKind.IMAGE_IMPORT,
                f"Import of image '{image.name}' failed",
                detail=str(exc),
            )
            return

        image.format = stored.format
        image.virtual_size_bytes = stored.virtual_size_bytes
        image.actual_size_bytes = stored.actual_size_bytes
        image.status = ImageStatus.AVAILABLE
        image.error_message = None
        session.add(image)
        session.commit()
        # Not about any one instance, so it carries no instance_id and appears
        # in the global feed only — see InstanceEvent.instance_id.
        record_event(
            EventKind.IMAGE_IMPORT,
            f"Imported image '{image.name}'",
            detail=(
                f"{image.format.value}, virtual {image.virtual_size_bytes} bytes, "
                f"cloud-init {'yes' if image.has_cloud_init else 'no'}"
            ),
        )
        logger.info(
            "Image '%s' available (%s, virtual %d bytes, probed as %s)",
            image.name, image.format.value, image.virtual_size_bytes, probe.format.value,
        )


@router.post(
    "/import",
    response_model=ImageRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Register a local disk image (async copy)",
)
def import_image(
    payload: ImageImportRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Image:
    existing = session.exec(select(Image).where(Image.name == payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"An image named '{payload.name}' already exists"
        )

    # Validate the path now so an obvious mistake is a 4xx rather than a row
    # that quietly turns Error a moment later.
    try:
        source = resolve_source_file(payload.path)
    except ImageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    image = Image(
        name=payload.name,
        filename=allocate_filename(payload.name, source),
        has_cloud_init=payload.has_cloud_init,
        status=ImageStatus.IMPORTING,
        project_id=resolve_project_id(session, payload.project_id),
    )
    session.add(image)
    session.commit()
    session.refresh(image)

    background_tasks.add_task(_import_job, image.id, str(source))
    logger.info("Accepted image import '%s' from %s", image.name, source)
    return image


def _fetch_job(image_id: str, url: str, sha256: str | None) -> None:
    """Background task: download from a URL, probe it, mark Available."""
    settings = get_settings()
    with Session(db_engine) as session:
        image = session.get(Image, image_id)
        if image is None:
            logger.warning("Fetch job: image %s vanished", image_id)
            return

        try:
            fetch_into_store(url, image.filename, expected_sha256=sha256, settings=settings)
            probed = probe_image(image_path(image, settings), settings)
        except ImageError as exc:
            logger.error("Fetch of '%s' failed: %s", image.name, exc)
            image.status = ImageStatus.ERROR
            image.error_message = str(exc)
            session.add(image)
            session.commit()
            record_event(
                EventKind.IMAGE_IMPORT,
                f"Download of image '{image.name}' failed",
                detail=str(exc),
            )
            return

        image.format = probed.format
        image.virtual_size_bytes = probed.virtual_size_bytes
        image.actual_size_bytes = probed.actual_size_bytes
        image.status = ImageStatus.AVAILABLE
        image.error_message = None
        session.add(image)
        session.commit()
        record_event(
            EventKind.IMAGE_IMPORT,
            f"Downloaded image '{image.name}'",
            detail=(
                f"From {url}\n"
                f"{image.format.value}, virtual {image.virtual_size_bytes} bytes, "
                f"checksum {'verified' if sha256 else 'not supplied'}"
            ),
        )
        logger.info("Image '%s' available from %s", image.name, url)


@router.post(
    "/fetch",
    response_model=ImageRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Download an image from a URL (async)",
)
def fetch_image(
    payload: ImageFetchRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> Image:
    """Register an image by URL and download it in the background.

    The import route that does not care what operating system the *customer*
    is running: every distribution publishes cloud images at a stable URL with
    a checksum beside them, and nothing here touches the caller's filesystem.

    Bounded by ``KURUKURU_IMAGE_FETCH_MAX_BYTES`` and, when a checksum is supplied,
    verified over the stream — see :func:`app.image_store.fetch_into_store`.
    """
    existing = session.exec(select(Image).where(Image.name == payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"An image named '{payload.name}' already exists"
        )

    # The filename is derived from the URL's last path segment purely for its
    # extension; the uuid prefix is what makes it collision-proof.
    suffix = Path(payload.url.split("?")[0]).suffix or ".img"
    image = Image(
        name=payload.name,
        filename=f"fetched-{uuid.uuid4().hex[:12]}{suffix}",
        has_cloud_init=payload.has_cloud_init,
        status=ImageStatus.IMPORTING,
        project_id=resolve_project_id(session, payload.project_id),
    )
    session.add(image)
    session.commit()
    session.refresh(image)

    background_tasks.add_task(_fetch_job, image.id, payload.url, payload.sha256)
    logger.info("Accepted image fetch '%s' from %s", image.name, payload.url)
    return image


@router.get("", response_model=list[ImageRead], summary="List images")
def list_images(
    project_id: str | None = Depends(project_filter),
    session: Session = Depends(get_session),
) -> list[Image]:
    stmt = select(Image)
    if project_id is not None:
        # The built-in image is deliberately not exempt. It belongs to the
        # default project like everything else, and a filtered view that
        # silently kept one row would be lying about what the filter does —
        # the launch modal handles an empty list by saying so.
        stmt = stmt.where(Image.project_id == project_id)
    return list(session.exec(stmt.order_by(Image.created_at)).all())


@router.get("/{image_id}", response_model=ImageRead, summary="Get one image")
def get_image(image_id: str, session: Session = Depends(get_session)) -> Image:
    return _get_or_404(session, image_id)


@router.delete(
    "/{image_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # 204 forbids a body, so the route must not declare a response model.
    response_class=Response,
    summary="Delete an image",
)
def delete_image(
    image_id: str,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    image = _get_or_404(session, image_id)

    if image.source is ImageSource.BUILTIN:
        raise HTTPException(
            status_code=409,
            detail="The built-in image cannot be deleted",
        )

    # Overlays keep a hard reference to their backing file. Removing it under a
    # live instance corrupts that instance's disk with no way to recover it.
    users = session.exec(
        select(Instance)
        .where(Instance.image_id == image_id)
        .where(Instance.status != InstanceStatus.TERMINATED)
    ).all()
    if users:
        names = ", ".join(sorted(i.name for i in users))
        raise HTTPException(
            status_code=409,
            detail=(
                f"Image '{image.name}' is in use by {len(users)} instance(s): {names}. "
                "Terminate them first."
            ),
        )

    delete_image_file(image, settings)
    session.delete(image)
    session.commit()
    logger.info("Deleted image '%s'", image.name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
