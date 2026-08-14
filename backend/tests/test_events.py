"""
The event log: what gets recorded, what cannot break because of it, and what
survives.

Two things are being asserted here that are easy to state and easy to lose.

First, that the log records the operations whose history nothing else keeps —
a restore (which changes no row at all), a reconciler correction (which nobody
asked for), and a second Error (which overwrites the first on the instance).

Second, that the log can never become the reason an operation fails. Every
event write is on a separate session and inside a blanket except; the test for
that breaks the writer deliberately and checks the terminate still succeeds.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session, select

import app.events as events_module
from app.events import prune_events, record_event
from app.models import (
    EventActor,
    EventKind,
    Instance,
    InstanceEvent,
    InstanceStatus,
    _utcnow,
)

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures
from tests.test_snapshots import snap_client  # noqa: F401 - a snapshot-capable engine


def _events(client, instance_id: str | None = None, **params) -> list[dict]:
    url = f"/instances/{instance_id}/events" if instance_id else "/events"
    r = client.get(url, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def _launch(client, name: str = "web-01") -> dict:
    r = client.post("/instances", json={"name": name})
    assert r.status_code == 202, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# The lifecycle, recorded
# --------------------------------------------------------------------------- #
def test_a_launch_records_creation_and_both_ends_of_provisioning(client):
    instance = _launch(client)

    kinds = _kinds(_events(client, instance["id"]))

    # Newest first.
    assert kinds == ["provisioning_succeeded", "provisioning_started", "created"]


def test_stop_and_start_both_survive_as_separate_entries(client):
    """The case that motivated the table.

    ``updated_at`` is one field: after the start, it holds only the start, and
    the stop that preceded it is unrecoverable. Here both are still present.
    """
    instance = _launch(client)
    client.post(f"/instances/{instance['id']}/stop")
    client.post(f"/instances/{instance['id']}/start")

    kinds = _kinds(_events(client, instance["id"]))

    assert kinds[:2] == ["started", "stopped"]


def test_the_creation_event_records_the_shape_that_was_asked_for(client):
    instance = _launch(client)

    created = [e for e in _events(client, instance["id"]) if e["kind"] == "created"][0]

    assert "1 vCPU / 1024 MB / 5 GB" in created["detail"]
    assert created["actor"] == "api"
    assert created["instance_name"] == "web-01"


def test_terminate_is_recorded_and_the_history_outlives_the_instance(client):
    """The row goes to Terminated; its history stays readable.

    An event log that disappeared with the resource would be useless for the
    only question anyone asks after a deletion: what happened to it.
    """
    instance = _launch(client)
    client.delete(f"/instances/{instance['id']}")

    events = _events(client, instance["id"])

    assert events[0]["kind"] == "terminated"
    assert "created" in _kinds(events)


def test_force_terminate_is_a_different_kind_and_says_what_was_left_behind(client):
    """A forced terminate may leave a VM running. That has to be in the record."""
    instance = _launch(client)

    from app.engines import ComputeEngineError

    def _boom(name):  # noqa: ANN001
        raise ComputeEngineError("hypervisor wedged")

    client.fake.destroy_instance = _boom
    r = client.delete(f"/instances/{instance['id']}", params={"force": True})
    assert r.status_code == 200

    latest = _events(client, instance["id"])[0]
    assert latest["kind"] == "force_terminated"
    assert "left behind" in latest["detail"]


def test_a_failed_launch_records_the_reason(client):
    """The reason is on the row too — but only until the next failure."""
    from app.engines import ComputeEngineError

    def _boom(**kwargs):  # noqa: ANN003
        raise ComputeEngineError("no disk space on the host")

    client.fake.provision_instance = _boom
    instance = _launch(client, "doomed")

    failed = _events(client, instance["id"])[0]
    assert failed["kind"] == "provisioning_failed"
    assert "no disk space" in failed["detail"]


def test_every_error_is_kept_not_just_the_most_recent(client):
    """``instances.error_message`` holds one message; the first is usually the
    one that explains the second."""
    from app.engines import ComputeEngineError

    instance = _launch(client)
    for message in ("first failure", "second failure"):
        def _boom(name, msg=message):  # noqa: ANN001
            raise ComputeEngineError(msg)

        client.fake.stop_instance = _boom
        # Put it back in a state stop is legal from.
        with Session(client.db_engine) as session:
            row = session.get(Instance, instance["id"])
            row.status = InstanceStatus.RUNNING
            session.add(row)
            session.commit()
        client.post(f"/instances/{instance['id']}/stop")

    details = [e["detail"] for e in _events(client, instance["id"]) if e["kind"] == "errored"]
    assert details == ["second failure", "first failure"]


# --------------------------------------------------------------------------- #
# Snapshots — restore is the one that changes no row
# --------------------------------------------------------------------------- #
def test_snapshot_create_restore_and_delete_are_all_recorded(snap_client):
    instance = _launch(snap_client)
    snap_client.post(f"/instances/{instance['id']}/stop")

    snap = snap_client.post(
        f"/instances/{instance['id']}/snapshots",
        json={"name": "baseline", "description": "before the upgrade"},
    ).json()
    snap_client.post(f"/instances/{instance['id']}/snapshots/{snap['id']}/restore")
    snap_client.delete(f"/instances/{instance['id']}/snapshots/{snap['id']}")

    kinds = _kinds(_events(snap_client, instance["id"]))
    assert kinds[:3] == ["snapshot_deleted", "snapshot_restored", "snapshot_created"]


def test_the_restore_event_says_what_was_discarded(snap_client):
    """Restore rewrites the disk and mutates nothing. Without this event there
    is no record anywhere that it happened."""
    instance = _launch(snap_client)
    snap_client.post(f"/instances/{instance['id']}/stop")
    snap = snap_client.post(
        f"/instances/{instance['id']}/snapshots", json={"name": "baseline"}
    ).json()

    snap_client.post(f"/instances/{instance['id']}/snapshots/{snap['id']}/restore")

    restored = _events(snap_client, instance["id"])[0]
    assert restored["kind"] == "snapshot_restored"
    assert "discarded" in restored["detail"]


def test_a_snapshot_that_failed_is_not_announced_as_created(snap_client):
    """The row says Error; the log must not claim a snapshot that never was."""
    instance = _launch(snap_client)
    snap_client.post(f"/instances/{instance['id']}/stop")

    snap_client.fake.fail_next = "qemu-img refused"
    snap_client.post(f"/instances/{instance['id']}/snapshots", json={"name": "doomed"})

    assert "snapshot_created" not in _kinds(_events(snap_client, instance["id"]))


# --------------------------------------------------------------------------- #
# The reconciler — the writer nothing else can see
# --------------------------------------------------------------------------- #
def test_an_out_of_band_stop_is_recorded_with_both_sides_of_the_change(client):
    """A VM stopped outside the API. Before this, the record simply changed."""
    instance = _launch(client)

    # The hypervisor now disagrees with the database, and nobody told us.
    client.fake.state[instance["name"]] = client.fake._info(
        instance["name"], InstanceStatus.STOPPED, running=False
    )
    client.post("/instances/refresh")

    latest = _events(client, instance["id"])[0]
    assert latest["kind"] == "reconciled"
    assert latest["actor"] == "reconciler"
    assert "Stopped" in latest["summary"] and "Running" in latest["summary"]
    assert "status: Running -> Stopped" in latest["detail"]


def test_a_vanished_vm_is_recorded_as_a_correction_to_error(client):
    instance = _launch(client)
    client.fake.state.pop(instance["name"])

    client.post("/instances/refresh")

    latest = _events(client, instance["id"])[0]
    assert latest["kind"] == "reconciled"
    assert "Error" in latest["summary"]
    assert "no longer exists" in latest["detail"]


def test_a_reconcile_pass_that_changes_nothing_records_nothing(client):
    """A 30-second timer must not produce 2,880 events a day."""
    instance = _launch(client)
    before = len(_events(client, instance["id"]))

    client.post("/instances/refresh")
    client.post("/instances/refresh")

    assert len(_events(client, instance["id"])) == before


def test_an_api_stop_is_not_also_reported_as_a_reconciler_correction(client):
    """Stop syncs the row from the hypervisor as part of its work. That sync is
    not an out-of-band change and must not be attributed to the reconciler."""
    instance = _launch(client)
    client.post(f"/instances/{instance['id']}/stop")

    actors = {e["actor"] for e in _events(client, instance["id"])}
    assert "reconciler" not in actors


# --------------------------------------------------------------------------- #
# Events must never become control flow
# --------------------------------------------------------------------------- #
def test_a_broken_event_writer_does_not_fail_the_operation(client, monkeypatch):
    """The property the blanket except exists for.

    If recording history can fail a terminate, the log has made the system less
    reliable than it was without one.
    """
    instance = _launch(client)

    def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("the event table is on fire")

    monkeypatch.setattr(events_module, "Session", _explode)

    r = client.delete(f"/instances/{instance['id']}")

    assert r.status_code == 200
    assert r.json()["status"] == "Terminated"


def test_record_event_swallows_everything(client):
    """Called directly, with an engine that cannot work."""
    record_event(EventKind.STARTED, "x", instance_id="nope", instance_name="nope")
    # No exception is the assertion; this also proves a null instance is fine.
    assert True


def test_a_long_detail_is_truncated_rather_than_stored_whole(client):
    instance = _launch(client)

    record_event(
        EventKind.ERRORED, "big", instance_id=instance["id"], detail="x" * 10_000
    )

    latest = _events(client, instance["id"])[0]
    assert latest["detail"].endswith("(truncated)")
    assert len(latest["detail"]) < 5000


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def test_the_global_feed_spans_instances_and_is_newest_first(client):
    first = _launch(client, "web-01")
    second = _launch(client, "web-02")

    feed = _events(client)

    names = [e["instance_name"] for e in feed]
    assert names[0] == "web-02"
    assert {first["name"], second["name"]} <= set(names)
    timestamps = [e["occurred_at"] for e in feed]
    assert timestamps == sorted(timestamps, reverse=True)


def test_the_feed_can_be_filtered_by_kind_and_instance(client):
    first = _launch(client, "web-01")
    _launch(client, "web-02")

    by_kind = _events(client, kind="created")
    assert _kinds(by_kind) == ["created", "created"]

    by_instance = _events(client, instance_id=first["id"])
    assert {e["instance_name"] for e in by_instance} == {"web-01"}


def test_pagination_walks_backwards_without_repeating_a_row(client):
    instance = _launch(client)
    client.post(f"/instances/{instance['id']}/stop")
    client.post(f"/instances/{instance['id']}/start")

    everything = _events(client, instance["id"])
    assert len(everything) >= 5

    page_one = _events(client, instance["id"], limit=2)
    page_two = _events(client, instance["id"], limit=2, before=page_one[-1]["occurred_at"])

    assert len(page_one) == 2
    assert page_one + page_two == everything[:4]


def test_an_unknown_instance_is_a_404_not_an_empty_list(client):
    r = client.get("/instances/does-not-exist/events")
    assert r.status_code == 404


def test_the_limit_is_capped(client):
    _launch(client)
    assert client.get("/events", params={"limit": 10_000}).status_code == 422
    assert client.get("/events", params={"limit": 0}).status_code == 422


def test_an_image_import_has_no_instance_and_still_reaches_the_global_feed(client):
    """Not every event belongs to an instance."""
    record_event(EventKind.IMAGE_IMPORT, "Imported image 'thing'")

    entry = _events(client, kind="image_import")[0]
    assert entry["instance_id"] is None
    assert entry["instance_name"] == ""


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
@pytest.fixture()
def event_db(client, monkeypatch):
    """The client fixture already points events at the test database."""
    monkeypatch.setattr(events_module, "db_engine", client.db_engine)
    return client.db_engine


def test_pruning_removes_only_what_has_aged_out(event_db):
    now = _utcnow()
    with Session(event_db) as session:
        session.add(
            InstanceEvent(
                kind=EventKind.CREATED, summary="old", occurred_at=now - timedelta(days=120)
            )
        )
        session.add(
            InstanceEvent(
                kind=EventKind.CREATED, summary="recent", occurred_at=now - timedelta(days=2)
            )
        )
        session.commit()

    assert prune_events(90) == 1

    with Session(event_db) as session:
        survivors = session.exec(select(InstanceEvent.summary)).all()
    assert "recent" in survivors and "old" not in survivors


def test_retention_of_zero_disables_pruning(event_db):
    with Session(event_db) as session:
        session.add(
            InstanceEvent(
                kind=EventKind.CREATED,
                summary="ancient",
                occurred_at=_utcnow() - timedelta(days=3650),
            )
        )
        session.commit()

    assert prune_events(0) == 0

    with Session(event_db) as session:
        assert session.exec(select(InstanceEvent)).all()


def test_a_terminated_instance_keeps_its_events_until_they_age_out(client, event_db):
    """Retention is by age alone, on purpose: the moment an instance is
    destroyed is the moment its history is most likely to be wanted."""
    instance = _launch(client)
    client.delete(f"/instances/{instance['id']}")

    assert prune_events(90) == 0
    assert len(_events(client, instance["id"])) >= 3


def test_the_actor_enum_covers_every_writer(client):
    """A guard on the vocabulary: if a new writer appears with no actor of its
    own it will show up here as an unexpected value rather than as 'api'."""
    instance = _launch(client)
    client.fake.state.pop(instance["name"])
    client.post("/instances/refresh")

    actors = {e["actor"] for e in _events(client)}
    assert actors <= {a.value for a in EventActor}
    assert {"api", "reconciler"} <= actors


def test_a_change_this_process_is_making_is_left_alone(client):
    """The reconciler and a running operation both watch the same change.

    Start blocks on QEMU for tens of seconds and the reconciler runs every 30,
    so the background pass routinely sees our own work. It must neither report
    it as an unexplained correction nor write its own (possibly already stale)
    view over the top of it.
    """
    from app.engines import get_engine_registry
    from app.routers.instances import _api_operation, reconcile_all

    with Session(client.db_engine) as session:
        instance = Instance(name="racing", status=InstanceStatus.STOPPED)
        session.add(instance)
        session.commit()
        session.refresh(instance)
        instance_id = instance.id

    client.fake.state["racing"] = client.fake._info(
        "racing", InstanceStatus.RUNNING, running=True
    )
    with _api_operation(instance_id), Session(client.db_engine) as session:
        reconcile_all(session, get_engine_registry())
        assert session.get(Instance, instance_id).status is InstanceStatus.STOPPED

    assert _kinds(_events(client, instance_id)) == []


def test_a_stop_landing_mid_pass_is_not_reverted(client):
    """The bug underneath the noisy line, and the reason `stop` was flaky.

    reconcile_all lists the hypervisor *before* it reads the rows. A stop that
    lands in that window used to be overwritten by a listing taken while the VM
    was still up: the row flipped back to Running a second after `stop`
    returned, and the next `start` was refused as "only Stopped instances can
    be started". Reproduced here by interleaving the two exactly as they raced
    live.
    """
    from app.engines import get_engine_registry
    from app.routers.instances import reconcile_all

    instance = _launch(client)  # Running, and the fake agrees

    real_list = client.fake.list_instances
    listed = {"done": False}

    def _list_then_stop():
        """Listing is the slow phase; the stop lands while it is in flight."""
        snapshot = real_list()
        if not listed["done"]:
            listed["done"] = True
            client.post(f"/instances/{instance['id']}/stop")
        return snapshot

    client.fake.list_instances = _list_then_stop
    with Session(client.db_engine) as session:
        reconcile_all(session, get_engine_registry())

    assert client.get(f"/instances/{instance['id']}").json()["status"] == "Stopped"


def test_the_claim_is_released_and_the_next_change_is_reported(client):
    """The suppression lasts exactly as long as the operation does."""
    from app.engines import get_engine_registry
    from app.routers.instances import _api_operation, reconcile_all

    with Session(client.db_engine) as session:
        instance = Instance(name="racing-two", status=InstanceStatus.STOPPED)
        session.add(instance)
        session.commit()
        session.refresh(instance)
        instance_id = instance.id

    client.fake.state["racing-two"] = client.fake._info(
        "racing-two", InstanceStatus.RUNNING, running=True
    )
    with _api_operation(instance_id), Session(client.db_engine) as session:
        reconcile_all(session, get_engine_registry())
    assert _kinds(_events(client, instance_id)) == []

    # Claim released. Someone now stops it from outside the API.
    client.fake.state["racing-two"] = client.fake._info(
        "racing-two", InstanceStatus.STOPPED, running=False
    )
    with Session(client.db_engine) as session:
        reconcile_all(session, get_engine_registry())

    assert _kinds(_events(client, instance_id)) == ["reconciled"]


def test_a_claim_is_released_even_when_the_operation_raises(client):
    """A leaked claim would silence that instance's corrections forever."""
    from app.routers.instances import _api_operation, _in_flight

    with pytest.raises(RuntimeError):
        with _api_operation("some-id"):
            raise RuntimeError("the hypervisor exploded")

    assert "some-id" not in _in_flight


def test_a_real_start_records_started_and_nothing_else(client):
    """End to end, through the route: one event, from the operation itself."""
    instance = _launch(client)
    client.post(f"/instances/{instance['id']}/stop")

    before = len(_events(client, instance["id"]))
    client.post(f"/instances/{instance['id']}/start")
    after = _events(client, instance["id"])

    assert len(after) == before + 1
    assert after[0]["kind"] == "started"
