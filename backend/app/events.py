"""
The instance event log — what happened, when, and who did it.

Every other table in this system holds *current* state. ``instances.updated_at``
is one mutable field, so a stop followed by a start leaves no trace of the stop,
and an Error overwrites whatever the row said before it. Restore is worse: it
rewrites a disk and, until this module existed, left no record anywhere that it
had run. This table is the history those rows cannot keep.

Three rules, and they are the whole design:

**Events are observability, not control flow.** :func:`record_event` cannot
raise. A full disk, a locked database, a bug in this file — none of them may
turn a successful terminate into a failed one. Every failure here is logged and
swallowed, which is the correct trade: a missing event is a gap in a history, a
raised exception is a broken operation.

**Write after the commit, never inside it.** Each event is written on its own
session so a caller's rollback cannot take it with it. The corollary is a rule
for callers: call this *after* your own ``session.commit()``, never before and
never in the middle. Doing it mid-transaction on SQLite means a second writer
waiting on the first — the event would be recorded late, or not at all, and the
lock wait would be charged to the operation it was describing.

**One writer per fact.** Events are emitted from the code that already performs
the mutation, so there is no second path that can drift. The reconciler is the
writer that matters most: an out-of-band correction is precisely the event
nothing else in this system can see.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlmodel import Session, delete, select

from app.database import engine as db_engine
from app.models import EventActor, EventKind, Instance, InstanceEvent, _utcnow

logger = logging.getLogger("iaas.events")

#: Detail is stored whole, but a runaway value (a driver that returns a
#: megabyte of stderr) would bloat every listing that reads it. Truncated with
#: a marker so it is obvious the text was cut rather than the error being odd.
_MAX_DETAIL_CHARS = 4000


def record_event(
    kind: EventKind,
    summary: str,
    *,
    instance: Instance | None = None,
    instance_id: str | None = None,
    instance_name: str = "",
    actor: EventActor = EventActor.API,
    detail: str | None = None,
) -> None:
    """Append one event. Never raises, never blocks the caller's transaction.

    Pass ``instance`` when you have the row; ``instance_id``/``instance_name``
    exist for the callers that only have the identifiers — a terminate that has
    already cleared its row, or a job whose instance vanished underneath it.
    """
    if instance is not None:
        instance_id = instance.id
        instance_name = instance.name

    try:
        if detail is not None and len(detail) > _MAX_DETAIL_CHARS:
            detail = detail[:_MAX_DETAIL_CHARS] + "\n... (truncated)"
        with Session(db_engine) as session:
            session.add(
                InstanceEvent(
                    instance_id=instance_id,
                    instance_name=instance_name or "",
                    kind=kind,
                    actor=actor,
                    summary=summary[:500],
                    detail=detail,
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - see the module docstring: never control flow
        logger.exception(
            "Could not record event %s for %s", kind.value, instance_name or instance_id
        )


def prune_events(retention_days: int, *, now: datetime | None = None) -> int:
    """Delete events older than the retention window. Returns how many.

    Runs at startup rather than on a timer. The table grows by a handful of rows
    per instance operation, so nothing accumulates fast enough to need a
    scheduler — and a local install that is running is a local install someone
    restarts.

    ``retention_days <= 0`` disables pruning entirely, for an operator who wants
    the full history and will manage the file themselves.

    Note that this prunes by age alone, deliberately: a terminated instance
    keeps every one of its events until they age out. The moment an instance is
    destroyed is the moment its history is most likely to be wanted, so tying
    retention to the instance's lifetime would delete exactly the wrong rows.
    """
    if retention_days <= 0:
        return 0

    cutoff = (now or _utcnow()) - timedelta(days=retention_days)
    try:
        with Session(db_engine) as session:
            doomed = session.exec(
                select(InstanceEvent.id).where(InstanceEvent.occurred_at < cutoff)
            ).all()
            if not doomed:
                return 0
            session.exec(
                delete(InstanceEvent).where(InstanceEvent.occurred_at < cutoff)
            )
            session.commit()
            logger.info(
                "Pruned %d event(s) older than %d days", len(doomed), retention_days
            )
            return len(doomed)
    except Exception:  # noqa: BLE001 - same reasoning as record_event
        logger.exception("Could not prune the event log")
        return 0
