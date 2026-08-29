"""
Volume snapshots: independent of instance snapshots, and gated on the one thing
that actually makes them unsafe.

Two assertions carry this file.

**A volume attached to a *stopped* instance can be snapshotted.** The rule is
not "detached" — it is "no running instance holds this file". Nothing has the
qcow2 open when the instance is stopped, so the copy is exactly as safe as one
taken while detached, and since detaching already requires a stop (DECISIONS
#22), demanding it first would add a stop/attach cycle and no safety at all.

**An instance snapshot and a volume snapshot are separate things.** Different
tables, different files, different lifecycles. Taking one does not take the
other, and a user who believes otherwise loses data silently. The routes are
asserted to be genuinely independent rather than assumed to be.
"""

from __future__ import annotations

from pathlib import Path

import pytest


from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures
from tests.test_volumes import _attach, _instance, _settings, _volume, vol_client  # noqa: F401


@pytest.fixture()
def snap_client(vol_client):  # noqa: F811
    """vol_client (which writes real volume files) behind a snapshot-capable
    engine. Reuses the instance-snapshot fake rather than a parallel one, so a
    test can assert the two kinds are independent while both go through the
    same engine."""
    import kurukuru.engines as engines_module
    from kurukuru.engines import EngineRegistry, get_engine_registry
    from kurukuru.main import app as api_app

    from tests.test_snapshots import SnapshottingFake

    fake = SnapshottingFake()
    vol_client.fake.__class__ = SnapshottingFake
    registry = EngineRegistry({"qemu": lambda: fake})
    engines_module._registry = registry
    api_app.dependency_overrides[get_engine_registry] = lambda: registry
    vol_client.fake = fake  # type: ignore[attr-defined]
    return vol_client


def _snapshot(client, volume_id: str, name: str = "before-upgrade", **extra) -> dict:
    """Create and return the *settled* row.

    The POST answers 202 with the row as written, which always says Creating —
    the background job runs after the response. Every caller here wants the
    outcome, so the re-read lives in the helper rather than in each test.
    """
    r = client.post(f"/volumes/{volume_id}/snapshots", json={"name": name, **extra})
    assert r.status_code == 202, r.text
    created = r.json()
    rows = client.get(f"/volumes/{volume_id}/snapshots").json()
    return next(s for s in rows if s["id"] == created["id"])


def _running_instance(client, name: str = "live-01") -> dict:
    """An instance left running — the state that must block a snapshot."""
    r = client.post("/instances", json={"name": name})
    assert r.status_code == 202, r.text
    return client.get(f"/instances/{r.json()['id']}").json()


# --------------------------------------------------------------------------- #
# The gate: a running instance, not attachment
# --------------------------------------------------------------------------- #
def test_a_detached_volume_can_be_snapshotted(snap_client):
    volume = _volume(snap_client, "data")
    snapshot = _snapshot(snap_client, volume["id"])

    assert snapshot["status"] == "Available"
    assert snapshot["volume_id"] == volume["id"]


def test_a_volume_attached_to_a_stopped_instance_can_be_snapshotted(snap_client):
    """The decision this feature turns on. Nothing holds the file open when the
    instance is stopped, so requiring a detach would be friction without
    safety."""
    volume = _volume(snap_client, "data")
    instance = _instance(snap_client)          # created, then stopped
    assert _attach(snap_client, volume["id"], instance["id"]).status_code == 200

    snapshot = _snapshot(snap_client, volume["id"])

    assert snapshot["status"] == "Available"
    still_attached = snap_client.get(f"/volumes/{volume['id']}").json()
    assert still_attached["attached_instance_name"] == instance["name"]


def test_a_snapshot_is_refused_while_a_running_instance_holds_it(snap_client):
    volume = _volume(snap_client, "data")
    instance = _running_instance(snap_client)
    snap_client.post(f"/instances/{instance['id']}/stop")
    assert _attach(snap_client, volume["id"], instance["id"]).status_code == 200
    snap_client.post(f"/instances/{instance['id']}/start")

    r = snap_client.post(f"/volumes/{volume['id']}/snapshots", json={"name": "nope"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert instance["name"] in detail          # names the blocker, not a rule
    assert "mid-write" in detail
    assert "can stay attached" in detail       # and names the actual fix


def test_a_restore_is_refused_while_a_running_instance_holds_it(snap_client):
    """Matters more than the snapshot guard: restoring under a live guest does
    not capture a bad copy, it replaces the filesystem the guest is using."""
    volume = _volume(snap_client, "data")
    instance = _instance(snap_client)
    assert _attach(snap_client, volume["id"], instance["id"]).status_code == 200
    snapshot = _snapshot(snap_client, volume["id"])
    snap_client.post(f"/instances/{instance['id']}/start")

    r = snap_client.post(
        f"/volumes/{volume['id']}/snapshots/{snapshot['id']}/restore"
    )

    assert r.status_code == 409
    assert instance["name"] in r.json()["detail"]


def test_a_stopped_instance_allows_a_restore(snap_client):
    volume = _volume(snap_client, "data")
    instance = _instance(snap_client)
    assert _attach(snap_client, volume["id"], instance["id"]).status_code == 200
    snapshot = _snapshot(snap_client, volume["id"])

    r = snap_client.post(f"/volumes/{volume['id']}/snapshots/{snapshot['id']}/restore")

    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------- #
# Independence from instance snapshots
# --------------------------------------------------------------------------- #
def test_volume_snapshots_and_instance_snapshots_are_separate(snap_client):
    """Two tables, two lists. Neither route may show the other's rows."""
    volume = _volume(snap_client, "data")
    instance = _instance(snap_client)
    assert _attach(snap_client, volume["id"], instance["id"]).status_code == 200

    _snapshot(snap_client, volume["id"], "volume-side")
    r = snap_client.post(
        f"/instances/{instance['id']}/snapshots", json={"name": "instance-side"}
    )
    assert r.status_code == 202, r.text

    vol_names = {s["name"] for s in
                 snap_client.get(f"/volumes/{volume['id']}/snapshots").json()}
    inst_names = {s["name"] for s in
                  snap_client.get(f"/instances/{instance['id']}/snapshots").json()}

    assert vol_names == {"volume-side"}
    assert inst_names == {"instance-side"}


def test_a_snapshot_belongs_to_one_volume_only(snap_client):
    a = _volume(snap_client, "vol-a")
    b = _volume(snap_client, "vol-b")
    snapshot = _snapshot(snap_client, a["id"], "only-on-a")

    assert snap_client.get(f"/volumes/{b['id']}/snapshots").json() == []
    r = snap_client.post(f"/volumes/{b['id']}/snapshots/{snapshot['id']}/restore")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_deleting_the_volume_drops_its_snapshot_rows(snap_client):
    """The snapshots lived inside the file that was just unlinked. Leaving the
    rows would advertise restore points whose data is already gone."""
    volume = _volume(snap_client, "data")
    _snapshot(snap_client, volume["id"], "one")
    _snapshot(snap_client, volume["id"], "two")

    assert snap_client.delete(f"/volumes/{volume['id']}").status_code == 204

    assert snap_client.get(f"/volumes/{volume['id']}/snapshots").status_code == 404


def test_a_duplicate_name_on_the_same_volume_is_refused(snap_client):
    volume = _volume(snap_client, "data")
    _snapshot(snap_client, volume["id"], "nightly")

    r = snap_client.post(f"/volumes/{volume['id']}/snapshots", json={"name": "nightly"})

    assert r.status_code == 409
    assert "nightly" in r.json()["detail"]


def test_the_same_name_on_two_volumes_is_fine(snap_client):
    a = _volume(snap_client, "vol-a")
    b = _volume(snap_client, "vol-b")

    _snapshot(snap_client, a["id"], "nightly")
    r = snap_client.post(f"/volumes/{b['id']}/snapshots", json={"name": "nightly"})

    assert r.status_code == 202, r.text


def test_a_name_that_would_break_qemu_img_is_refused(snap_client):
    volume = _volume(snap_client, "data")
    for bad in ("-leading-dash", "with\nnewline", "   "):
        r = snap_client.post(f"/volumes/{volume['id']}/snapshots", json={"name": bad})
        assert r.status_code == 422, (bad, r.status_code)


def test_restoring_a_snapshot_that_is_not_available_is_refused(snap_client):
    """A failed snapshot is not a restore point, and must not read as one."""
    volume = _volume(snap_client, "data")
    snap_client.fake.fail_next = "qemu-img: no space left on device"
    r = snap_client.post(f"/volumes/{volume['id']}/snapshots", json={"name": "doomed"})
    assert r.status_code == 202, r.text
    snapshot = snap_client.get(f"/volumes/{volume['id']}/snapshots").json()[0]
    assert snapshot["status"] == "Error"
    assert "no space left" in snapshot["error_message"]

    r = snap_client.post(f"/volumes/{volume['id']}/snapshots/{snapshot['id']}/restore")

    assert r.status_code == 409
    assert "not Available" in r.json()["detail"]


def test_deleting_a_snapshot_removes_the_row(snap_client):
    volume = _volume(snap_client, "data")
    snapshot = _snapshot(snap_client, volume["id"], "scratch")

    r = snap_client.delete(f"/volumes/{volume['id']}/snapshots/{snapshot['id']}")
    assert r.status_code == 202, r.text

    assert snap_client.get(f"/volumes/{volume['id']}/snapshots").json() == []


def test_a_missing_volume_is_404_not_500(snap_client):
    assert snap_client.get("/volumes/nope/snapshots").status_code == 404
    assert snap_client.post(
        "/volumes/nope/snapshots", json={"name": "x"}
    ).status_code == 404
