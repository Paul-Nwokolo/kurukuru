"""
Reading the event log.

Two views of one table: a global feed and a per-instance history. Both are
read-only — there is no endpoint that writes an event, and deliberately so. An
event describes something this system did; one that could be posted from outside
would describe something it might not have.

Ordering is newest-first everywhere, because the question is nearly always "what
just happened". Pagination is keyset rather than offset (``before`` is a
timestamp, not a page number): the feed grows at the head, so an offset page 2
would shift under the reader between requests and show them the same row twice.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, col, select

from kurukuru.database import get_session
from kurukuru.models import EventKind, Instance, InstanceEvent, InstanceEventRead

logger = logging.getLogger("kurukuru.events")

router = APIRouter(tags=["events"])

#: A page size a dashboard can render and a terminal can scroll. The hard cap
#: matters more than the default: this table is the one place in the API where
#: a single request could otherwise ask for a hundred thousand rows.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def _page(
    session: Session,
    *,
    instance_id: str | None,
    kind: EventKind | None,
    limit: int,
    before: datetime | None,
) -> list[InstanceEvent]:
    """One page of events, newest first."""
    stmt = select(InstanceEvent)
    if instance_id is not None:
        stmt = stmt.where(InstanceEvent.instance_id == instance_id)
    if kind is not None:
        stmt = stmt.where(InstanceEvent.kind == kind)
    if before is not None:
        # Strictly before, so passing the last row's timestamp back never
        # repeats it. Two events in the same microsecond would be lost across
        # a page boundary; they are ordered by a clock with microsecond
        # resolution and written one operation at a time, so that is theory.
        stmt = stmt.where(col(InstanceEvent.occurred_at) < before)
    stmt = stmt.order_by(col(InstanceEvent.occurred_at).desc()).limit(limit)
    return list(session.exec(stmt).all())


@router.get(
    "/events",
    response_model=list[InstanceEventRead],
    summary="Global event feed",
)
def list_events(
    instance_id: str | None = Query(None, description="Only events for this instance"),
    kind: EventKind | None = Query(None, description="Only events of this kind"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    before: datetime | None = Query(
        None, description="Only events strictly older than this timestamp (UTC)"
    ),
    session: Session = Depends(get_session),
) -> list[InstanceEvent]:
    """Everything that has happened, newest first.

    Includes events with no ``instance_id`` — an image import is not about any
    one instance but is still part of the history of the install.
    """
    return _page(
        session, instance_id=instance_id, kind=kind, limit=limit, before=before
    )


@router.get(
    "/instances/{instance_id}/events",
    response_model=list[InstanceEventRead],
    summary="One instance's history",
)
def list_instance_events(
    instance_id: str,
    kind: EventKind | None = Query(None, description="Only events of this kind"),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    before: datetime | None = Query(
        None, description="Only events strictly older than this timestamp (UTC)"
    ),
    session: Session = Depends(get_session),
) -> list[InstanceEvent]:
    """This instance's history, newest first.

    404s on an unknown instance rather than returning an empty list, so a typo
    in an id is distinguishable from an instance that genuinely has no events.
    Terminated instances still answer: their row is retained, and their history
    is retained with it until it ages out.
    """
    if session.get(Instance, instance_id) is None:
        raise HTTPException(status_code=404, detail=f"Instance '{instance_id}' not found")
    return _page(
        session, instance_id=instance_id, kind=kind, limit=limit, before=before
    )
