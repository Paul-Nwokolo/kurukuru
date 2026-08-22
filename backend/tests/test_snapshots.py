"""
Snapshots: the stopped-only rule, the lifecycle, and what happens around it.

The rule that an instance must be stopped is the load-bearing one here, and it
is a decision taken on measurement — WHPX refuses to save VM state, and the
live disk-only mechanism records zero bytes of it. The tests below pin the rule
and, as importantly, the *reason* reaching the user: a 409 that says why beats
one that says no.
"""

from __future__ import annotations

from pathlib import Path

from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from app.engines import ComputeEngineError, SnapshotInfo
from app.engines.qemu import _parse_snapshot_list
from app.models import Instance, InstanceStatus, Snapshot, SnapshotStatus

from tests.test_instances_api import FakeQemuEngine, client, iso_dir  # noqa: F401


class SnapshottingFake(FakeQemuEngine):
    """Fake with the snapshot half of the interface implemented in memory."""

    supports_snapshots = True
    snapshots_require_stopped = True

    def __init__(self, name: str = "qemu") -> None:
        super().__init__(name)
        self.snaps: dict[str, list[SnapshotInfo]] = {}
        self.volume_snaps: dict[str, list[SnapshotInfo]] = {}
        self.fail_next: str | None = None

    def create_snapshot(self, name, tag):
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise ComputeEngineError(message)
        info = SnapshotInfo(
            tag=tag, size_bytes=0, created_at=datetime.now(timezone.utc), hypervisor_id="1"
        )
        self.snaps.setdefault(name, []).append(info)
        return info

    def list_snapshots(self, name):
        return list(self.snaps.get(name, []))

    def restore_snapshot(self, name, tag):
        if tag not in {s.tag for s in self.snaps.get(name, [])}:
            raise ComputeEngineError(f"no snapshot {tag}")
        self._record("restore", name)

    def delete_snapshot(self, name, tag):
        self.snaps[name] = [s for s in self.snaps.get(name, []) if s.tag != tag]

    # -- volume snapshots -------------------------------------------------- #
    # Keyed by path rather than by name, because that is how a volume is
    # identified on disk. Kept in this fake rather than a second one so that a
    # test can assert instance and volume snapshots really are independent
    # while both go through the same engine.
    supports_volume_snapshots = True

    def create_volume_snapshot(self, volume_path, tag):
        if self.fail_next:
            message, self.fail_next = self.fail_next, None
            raise ComputeEngineError(message)
        if not Path(volume_path).exists():
            raise ComputeEngineError(f"No volume file at {volume_path}")
        info = SnapshotInfo(
            tag=tag, size_bytes=0, created_at=datetime.now(timezone.utc), hypervisor_id="1"
        )
        self.volume_snaps.setdefault(str(volume_path), []).append(info)
        return info

    def list_volume_snapshots(self, volume_path):
        return list(self.volume_snaps.get(str(volume_path), []))

    def restore_volume_snapshot(self, volume_path, tag):
        if tag not in {s.tag for s in self.volume_snaps.get(str(volume_path), [])}:
            raise ComputeEngineError(f"no volume snapshot {tag}")

    def delete_volume_snapshot(self, volume_path, tag):
        key = str(volume_path)
        self.volume_snaps[key] = [
            s for s in self.volume_snaps.get(key, []) if s.tag != tag
        ]


@pytest.fixture()
def snap_client(client):
    """The standard client, with a snapshot-capable engine behind it."""
    fake = SnapshottingFake()
    client.fake.__class__ = SnapshottingFake  # keep identity for other helpers
    from app.engines import EngineRegistry
    import app.engines as engines_module

    registry = EngineRegistry({"qemu": lambda: fake})
    engines_module._registry = registry
    from app.engines import get_engine_registry
    from app.main import app as api_app

    api_app.dependency_overrides[get_engine_registry] = lambda: registry
    client.fake = fake  # type: ignore[attr-defined]
    return client


def _stopped_instance(client, name: str = "snapme") -> str:
    """Launch and stop an instance, returning its id."""
    body = client.post("/instances", json={"name": name, "flavor": "small"}).json()
    client.post(f"/instances/{body['id']}/stop")
    return body["id"]


# --------------------------------------------------------------------------- #
# The stopped-only rule
# --------------------------------------------------------------------------- #
def test_snapshotting_a_running_instance_is_refused_with_the_reason(snap_client):
    """The 409 has to explain itself — the user did nothing wrong.

    WHPX cannot save a running guest's memory, and taking its disk underneath
    it would capture a filesystem mid-write. "Stop it first" alone invites
    "why?", and the answer is not something they can fix.
    """
    body = snap_client.post("/instances", json={"name": "live-one", "flavor": "small"}).json()

    r = snap_client.post(f"/instances/{body['id']}/snapshots", json={"name": "nope"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "Running" in detail
    assert "memory" in detail and "mid-write" in detail


def test_restoring_a_running_instance_is_refused(snap_client):
    body = snap_client.post("/instances", json={"name": "live-two", "flavor": "small"}).json()
    snap_client.post(f"/instances/{body['id']}/stop")
    snap = snap_client.post(
        f"/instances/{body['id']}/snapshots", json={"name": "s1"}
    ).json()
    snap_client.post(f"/instances/{body['id']}/start")

    r = snap_client.post(f"/instances/{body['id']}/snapshots/{snap['id']}/restore")
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def test_create_returns_202_and_settles_available(snap_client):
    instance_id = _stopped_instance(snap_client)

    r = snap_client.post(
        f"/instances/{instance_id}/snapshots",
        json={"name": "clean", "description": "before the experiment"},
    )
    assert r.status_code == 202

    rows = snap_client.get(f"/instances/{instance_id}/snapshots").json()
    assert len(rows) == 1
    assert rows[0]["status"] == "Available"
    assert rows[0]["description"] == "before the experiment"


def test_a_failing_hypervisor_leaves_the_row_in_error_with_the_message(snap_client):
    instance_id = _stopped_instance(snap_client)
    snap_client.fake.fail_next = "qemu-img: Could not create snapshot: No space left"

    snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "doomed"})

    row = snap_client.get(f"/instances/{instance_id}/snapshots").json()[0]
    assert row["status"] == "Error"
    assert "No space left" in row["error_message"]


def test_duplicate_snapshot_names_are_refused_per_instance(snap_client):
    instance_id = _stopped_instance(snap_client)
    snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "dupe"})

    r = snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "dupe"})
    assert r.status_code == 409


def test_the_same_name_on_two_instances_is_fine(snap_client):
    """Names are scoped to their instance, like snapshots on any hypervisor."""
    first = _stopped_instance(snap_client, "host-a")
    second = _stopped_instance(snap_client, "host-b")

    assert snap_client.post(f"/instances/{first}/snapshots", json={"name": "base"}).status_code == 202
    assert snap_client.post(f"/instances/{second}/snapshots", json={"name": "base"}).status_code == 202


def test_restore_returns_the_disk_and_leaves_the_snapshot(snap_client):
    instance_id = _stopped_instance(snap_client)
    snap = snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "base"}).json()

    r = snap_client.post(f"/instances/{instance_id}/snapshots/{snap['id']}/restore")

    assert r.status_code == 200
    assert ("restore", "snapme") in snap_client.fake.calls
    # Restoring is not consuming: the snapshot is still there to restore again.
    assert len(snap_client.get(f"/instances/{instance_id}/snapshots").json()) == 1


def test_restoring_a_snapshot_that_is_still_creating_is_refused(snap_client):
    instance_id = _stopped_instance(snap_client)
    with Session(snap_client.db_engine) as session:  # type: ignore[attr-defined]
        row = Snapshot(instance_id=instance_id, name="half", status=SnapshotStatus.CREATING)
        session.add(row)
        session.commit()
        snapshot_id = row.id

    r = snap_client.post(f"/instances/{instance_id}/snapshots/{snapshot_id}/restore")
    assert r.status_code == 409
    assert "Creating" in r.json()["detail"]


def test_delete_removes_it_from_the_hypervisor_and_the_catalog(snap_client):
    instance_id = _stopped_instance(snap_client)
    snap = snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "gone"}).json()

    r = snap_client.delete(f"/instances/{instance_id}/snapshots/{snap['id']}")

    assert r.status_code == 202
    assert snap_client.get(f"/instances/{instance_id}/snapshots").json() == []
    assert snap_client.fake.snaps.get("snapme") == []


def test_snapshots_of_another_instance_are_not_reachable(snap_client):
    """The id is scoped by its instance; a stray id must 404, not act."""
    first = _stopped_instance(snap_client, "owner")
    second = _stopped_instance(snap_client, "stranger")
    snap = snap_client.post(f"/instances/{first}/snapshots", json={"name": "mine"}).json()

    assert snap_client.get(f"/instances/{second}/snapshots").json() == []
    r = snap_client.post(f"/instances/{second}/snapshots/{snap['id']}/restore")
    assert r.status_code == 404


def test_terminating_an_instance_takes_its_snapshots_with_it(snap_client):
    """They live inside the overlay, which is deleted with the instance.

    Leaving the rows would advertise restore points whose data is gone.
    """
    instance_id = _stopped_instance(snap_client)
    snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": "doomed"})
    assert len(snap_client.get(f"/instances/{instance_id}/snapshots").json()) == 1

    snap_client.delete(f"/instances/{instance_id}")

    with Session(snap_client.db_engine) as session:  # type: ignore[attr-defined]
        remaining = session.exec(
            select(Snapshot).where(Snapshot.instance_id == instance_id)
        ).all()
    assert remaining == []


def test_snapshots_on_an_unknown_instance_404(snap_client):
    assert snap_client.get("/instances/nope/snapshots").status_code == 404
    assert snap_client.post("/instances/nope/snapshots", json={"name": "x"}).status_code == 404


# --------------------------------------------------------------------------- #
# Name validation — the tag goes to qemu-img as a positional argument
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["-rf", "with\nnewline", "with\ttab", "   "])
def test_names_that_would_confuse_qemu_img_are_refused(snap_client, name):
    instance_id = _stopped_instance(snap_client)
    r = snap_client.post(f"/instances/{instance_id}/snapshots", json={"name": name})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Parsing qemu-img's output
# --------------------------------------------------------------------------- #
def test_snapshot_list_parsing():
    """Real `qemu-img snapshot -l` output, including a tag containing a space."""
    parsed = _parse_snapshot_list(
        "Snapshot list:\n"
        "ID      TAG               VM_SIZE                DATE        VM_CLOCK     ICOUNT\n"
        "1       clean-state           0 B 2026-08-12 00:10:29  0000:00:00.000          0\n"
        "2       after change      1.5 MiB 2026-08-12 00:11:02  0000:00:00.000          0\n"
    )

    assert [s.tag for s in parsed] == ["clean-state", "after change"]
    assert parsed[0].size_bytes == 0
    assert parsed[1].size_bytes == 1572864
    assert parsed[0].hypervisor_id == "1"
    assert parsed[0].created_at is not None


def test_parsing_tolerates_no_snapshots():
    assert _parse_snapshot_list("") == []
    assert _parse_snapshot_list("Snapshot list:\nID TAG VM_SIZE DATE VM_CLOCK\n") == []


# --------------------------------------------------------------------------- #
# The default engine says no, rather than raising NotImplementedError at users
# --------------------------------------------------------------------------- #
def test_an_engine_without_snapshot_support_refuses_cleanly(client):
    """The stock fake has supports_snapshots False, like any new driver."""
    body = client.post("/instances", json={"name": "plain", "flavor": "small"}).json()
    client.post(f"/instances/{body['id']}/stop")

    r = client.post(f"/instances/{body['id']}/snapshots", json={"name": "x"})

    assert r.status_code == 409
    assert "does not support snapshots" in r.json()["detail"]
