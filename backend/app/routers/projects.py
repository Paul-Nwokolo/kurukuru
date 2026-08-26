"""
Projects: grouping, not tenancy.

A project is a label. It groups instances, images and key pairs so a dashboard
with three clients' work in it is not one flat list. That is the whole feature.

**A project is not a security boundary and must never be described as one.**
There is no authentication in this system, so there is nothing to isolate from:
anything that can reach the port can list every project, read every resource and
act on all of it. Filtering by project changes what a *view* shows, not what a
caller may do. Every message in this module is worded with that in mind, and any
future one should be too — a boundary users believe in but the system does not
enforce is worse than no boundary at all.

Instance names stay globally unique regardless of project. The name is the
hypervisor's identity — it is the on-disk directory, the QEMU process label, the
cloud-init ``instance-id`` and the guest hostname — so two projects each holding
a ``web`` would share one ``disk.qcow2`` and one set of ports. See DECISIONS #20
for the measurements.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlmodel import Session, select

from app.models import (
    DEFAULT_PROJECT_NAME,
    Image,
    Instance,
    InstanceStatus,
    KeyPair,
    Project,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
)
from app.database import engine as db_engine
from app.database import get_session

logger = logging.getLogger("kurukuru.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

#: Resource tables carrying a project_id, and the attribute to filter on. Kept
#: as data so "move everything out of this project" and "count what is in it"
#: cannot disagree, and so adding a resource type next phase is one line.
_OWNED = ((Instance, "instances"), (Image, "images"), (KeyPair, "keypairs"))


def default_project(session: Session) -> Project | None:
    """The project resources fall back to. Seeded by the migration."""
    return session.exec(select(Project).where(Project.is_default)).first()


def resolve_project_id(session: Session, project_id: str | None) -> str | None:
    """Validate a caller-supplied project, or fall back to the default.

    Returns the id to store. A null request means "you decide", and the answer
    is the default project — which is what every resource created before
    projects existed was migrated into, so the behaviour is unchanged for
    anyone who never uses the feature.
    """
    if project_id is None:
        fallback = default_project(session)
        return fallback.id if fallback is not None else None

    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=422, detail=f"Unknown project '{project_id}'")
    return project_id


def _counts(session: Session, project_id: str) -> dict[str, int]:
    """What is filed under a project. Live instances only.

    Terminated rows are audit history: counting them would show a project as
    full of instances that no longer exist, and — worse — would make a project
    look undeletable long after everything in it was destroyed.
    """
    instances = session.exec(
        select(Instance)
        .where(Instance.project_id == project_id)
        .where(Instance.status != InstanceStatus.TERMINATED)
    ).all()
    images = session.exec(select(Image).where(Image.project_id == project_id)).all()
    keypairs = session.exec(select(KeyPair).where(KeyPair.project_id == project_id)).all()
    return {
        "instance_count": len(instances),
        "image_count": len(images),
        "keypair_count": len(keypairs),
    }


def _read(session: Session, project: Project) -> ProjectRead:
    return ProjectRead(
        id=project.id,
        name=project.name,
        description=project.description,
        is_default=project.is_default,
        created_at=project.created_at,
        **_counts(session, project.id),
    )


def _get_or_404(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("", response_model=list[ProjectRead], summary="List projects")
def list_projects(session: Session = Depends(get_session)) -> list[ProjectRead]:
    """Every project, default first, then oldest to newest.

    The default leads because it is where everything starts and where anything
    orphaned ends up, so it is the one a reader most often wants.
    """
    projects = session.exec(select(Project).order_by(Project.created_at)).all()
    ordered = sorted(projects, key=lambda p: (not p.is_default, p.created_at))
    return [_read(session, project) for project in ordered]


@router.get("/{project_id}", response_model=ProjectRead, summary="Get one project")
def get_project(project_id: str, session: Session = Depends(get_session)) -> ProjectRead:
    return _read(session, _get_or_404(session, project_id))


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
def create_project(
    payload: ProjectCreate, session: Session = Depends(get_session)
) -> ProjectRead:
    existing = session.exec(select(Project).where(Project.name == payload.name)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"A project named '{payload.name}' already exists"
        )

    project = Project(name=payload.name, description=payload.description)
    session.add(project)
    session.commit()
    session.refresh(project)
    logger.info("Created project '%s' -> %s", project.name, project.id)
    return _read(session, project)


@router.patch("/{project_id}", response_model=ProjectRead, summary="Rename a project")
def update_project(
    project_id: str, payload: ProjectUpdate, session: Session = Depends(get_session)
) -> ProjectRead:
    """Rename or re-describe. Nothing else about a project is mutable.

    ``is_default`` is not settable: moving it would strand the resources that
    fall back to it, and there is no use case for a second one.
    """
    project = _get_or_404(session, project_id)

    if payload.name is not None and payload.name != project.name:
        clash = session.exec(select(Project).where(Project.name == payload.name)).first()
        if clash is not None:
            raise HTTPException(
                status_code=409, detail=f"A project named '{payload.name}' already exists"
            )
        project.name = payload.name
    if payload.description is not None:
        project.description = payload.description

    session.add(project)
    session.commit()
    session.refresh(project)
    return _read(session, project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a project, moving what remains to the default",
)
def delete_project(
    project_id: str, session: Session = Depends(get_session)
) -> Response:
    """Delete the label. Never the resources.

    Refused while live instances are filed under it, and the message names
    them: deleting a project is a filing decision, and quietly re-filing a
    running VM as a side effect of tidying up the sidebar is not something a
    user asked for. Terminate or move them first.

    Images and key pairs do not block — they are inert — but they are *moved*
    to the default project rather than deleted. Deleting a 4 GB image because
    someone removed a label it happened to carry would be indefensible.
    """
    project = _get_or_404(session, project_id)

    if project.is_default:
        raise HTTPException(
            status_code=409,
            detail=(
                "The default project cannot be deleted — it is where resources "
                "from any other deleted project are moved."
            ),
        )

    live = session.exec(
        select(Instance)
        .where(Instance.project_id == project_id)
        .where(Instance.status != InstanceStatus.TERMINATED)
    ).all()
    if live:
        names = ", ".join(sorted(instance.name for instance in live))
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{project.name}' still holds {len(live)} instance(s): {names}. "
                "Terminate them or move them to another project first."
            ),
        )

    fallback = default_project(session)
    if fallback is None:  # pragma: no cover - the migration guarantees one
        raise HTTPException(
            status_code=409, detail="No default project exists to move resources into"
        )

    moved = 0
    for model, _label in _OWNED:
        for row in session.exec(
            select(model).where(model.project_id == project_id)
        ).all():
            row.project_id = fallback.id
            session.add(row)
            moved += 1

    session.delete(project)
    session.commit()
    logger.info(
        "Deleted project '%s'; moved %d resource(s) to '%s'",
        project.name, moved, fallback.name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def project_filter(
    project_id: str | None = Query(
        None,
        description=(
            "Only resources filed under this project. Organisational only — "
            "omitting it lists everything, and there is no permission attached."
        ),
    ),
) -> str | None:
    """Shared query parameter, so every list endpoint documents it identically."""
    return project_id


def ensure_default_project() -> None:
    """Startup hook: guarantee a default project exists.

    The migration seeds it, but a database created by ``create_all`` alone —
    which is what every test fixture does — never runs the migration. Without
    this, the first launch in a fresh install would file its instance under no
    project at all.
    """
    with Session(db_engine) as session:
        if default_project(session) is not None:
            return
        project = Project(
            name=DEFAULT_PROJECT_NAME,
            description="Everything that has not been filed anywhere else.",
            is_default=True,
        )
        session.add(project)
        session.commit()
        logger.info("Created the default project")
