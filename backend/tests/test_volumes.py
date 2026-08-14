"""
Volumes: the two guarantees, and the rules that protect them.

Two assertions in this file are load-bearing and everything else supports them.

**Terminating an instance must not destroy a volume.** A volume is a separate
file that outlives the instances it is attached to; destroying one because
someone destroyed the VM it happened to be plugged into is the one unforgivable
bug in this feature. Tested against the real file on disk, not just the row.

**Attached order must survive a restart**, because the guest's `/dev/vdb`,
`/dev/vdc` … follow the order of the engine's `-drive` arguments. A reordering
silently renames the guest's disks and breaks an `/etc/fstab` written against
the old names.

The stopped-only rule exists because QEMU's hot-unplug was measured removing a
device from under a mounted filesystem in under a second while reporting
success (DECISIONS #22). The 409s here are that measurement, enforced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.models import Instance, InstanceStatus, Volume, VolumeStatus

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures


@pytest.fixture()
def vol_client(client, monkeypatch, tmp_path):
    """The standard client, with volume creation writing real qcow2 files.

    The create job shells out to ``qemu-img``; here it writes a small sparse
    file instead, so the suite keeps running on a host without QEMU while the
    file still genuinely exists and can genuinely be deleted.
    """
    import app.routers.volumes as volumes_module

    def _fake_create(self, path, size_gb):  # noqa: ANN001
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"QFI\xfb" + b"\0" * 60)  # a qcow2 magic header

    from app.engines.qemu import QemuEngine

    monkeypatch.setattr(QemuEngine, "create_blank_disk", _fake_create)
    monkeypatch.setattr(volumes_module, "get_settings", lambda: _settings(tmp_path))
    return client


def _settings(tmp_path):
    from app.config import Settings

    return Settings(state_dir=str(tmp_path / "volstate"))


def _volume(client, name: str = "data", size_gb: int = 1, **extra) -> dict:
    r = client.post("/volumes", json={"name": name, "size_gb": size_gb, **extra})
    assert r.status_code == 202, r.text
    return client.get(f"/volumes/{r.json()['id']}").json()


def _instance(client, name: str = "web-01") -> dict:
    r = client.post("/instances", json={"name": name})
    assert r.status_code == 202, r.text
    client.post(f"/instances/{r.json()['id']}/stop")
    return client.get(f"/instances/{r.json()['id']}").json()


def _attach(client, volume_id: str, instance_id: str):
    return client.post(f"/volumes/{volume_id}/attach", json={"instance_id": instance_id})


# --------------------------------------------------------------------------- #
# Guarantee 1: terminating an instance never destroys a volume
# --------------------------------------------------------------------------- #
def test_terminating_an_instance_detaches_its_volumes_and_keeps_them(vol_client):
    """The one unforgivable bug. Asserted on the row *and* the file."""
    volume = _volume(vol_client, "irreplaceable")
    instance = _instance(vol_client)
    assert _attach(vol_client, volume["id"], instance["id"]).status_code == 200
    path = Path(vol_client.get(f"/volumes/{volume['id']}").json()["path"])
    assert path.exists()

    vol_client.delete(f"/instances/{instance['id']}")

    survivor = vol_client.get(f"/volumes/{volume['id']}")
    assert survivor.status_code == 200
    assert survivor.json()["status"] == "Available"
    assert survivor.json()["attached_instance_id"] is None
    assert path.exists(), "the volume file was deleted with the instance"
    assert path.read_bytes().startswith(b"QFI\xfb"), "the volume's data was touched"


def test_the_terminate_event_says_the_volumes_were_kept(vol_client):
    """So the history does not read as though they went with the instance."""
    volume = _volume(vol_client, "kept")
    instance = _instance(vol_client)
    _attach(vol_client, volume["id"], instance["id"])

    vol_client.delete(f"/instances/{instance['id']}")

    events = vol_client.get(f"/instances/{instance['id']}/events").json()
    terminated = next(e for e in events if e["kind"] == "terminated")
    assert "detached and kept" in terminated["detail"]
    assert "kept" in terminated["detail"]


def test_a_detached_volume_can_be_attached_to_a_different_instance(vol_client):
    """The point of surviving: the data moves to its replacement."""
    volume = _volume(vol_client, "portable")
    first = _instance(vol_client, "web-01")
    _attach(vol_client, volume["id"], first["id"])
    vol_client.delete(f"/instances/{first['id']}")

    second = _instance(vol_client, "web-02")
    r = _attach(vol_client, volume["id"], second["id"])

    assert r.status_code == 200
    assert r.json()["attached_instance_name"] == "web-02"


# --------------------------------------------------------------------------- #
# Guarantee 2: attach order is stable
# --------------------------------------------------------------------------- #
def test_attach_order_decides_the_drive_arguments(vol_client):
    """The guest names its disks in the order QEMU is given them."""
    instance = _instance(vol_client)
    for name in ("first", "second", "third"):
        _attach(vol_client, _volume(vol_client, name)["id"], instance["id"])

    with Session(vol_client.db_engine) as session:
        from app.routers.volumes import _attached_paths

        paths = _attached_paths(session, instance["id"])
        names = [
            session.exec(select(Volume).where(Volume.path == p)).one().name for p in paths
        ]

    assert names == ["first", "second", "third"]


def test_device_hints_follow_the_order(vol_client):
    instance = _instance(vol_client)
    hints = []
    for name in ("first", "second", "third"):
        hints.append(_attach(vol_client, _volume(vol_client, name)["id"], instance["id"]).json()["device_hint"])

    assert hints == ["vdb", "vdc", "vdd"]


def test_a_volume_carries_a_name_for_each_guest_that_could_see_it(vol_client):
    """``vdb`` is a *Linux* name, and the UI printed it whatever the guest was.

    A Windows guest has no ``/dev`` and never will, so a volume attached to one
    advertised a path that guest has never heard of and sent the user looking
    for it. Both spellings are served, and they are the same disk counted the
    same way: the root disk is ``vda`` / Disk 0, so the first attachment is
    ``vdb`` / Disk 1.
    """
    instance = _instance(vol_client)
    attached = _attach(
        vol_client, _volume(vol_client, "data")["id"], instance["id"]
    ).json()

    assert attached["device_hint"] == "vdb"
    assert attached["windows_disk_hint"] == "Disk 1"


def test_an_attachment_reports_which_guest_is_looking_at_it(vol_client):
    """Which of the two names to show is not the client's to guess.

    Windows instances cannot be provisioned yet, so the row is written
    directly — the point under test is the *serialisation*, not the launch
    path, and waiting for provisioning to exist would leave the UI free to
    keep printing ``/dev/vdb`` at Windows users in the meantime.
    """
    from app.models import GuestOS

    instance = _instance(vol_client)
    volume = _volume(vol_client, "data")
    _attach(vol_client, volume["id"], instance["id"])

    linux = vol_client.get(f"/volumes/{volume['id']}").json()
    assert linux["attached_instance_guest_os"] == "linux"

    with Session(vol_client.db_engine) as session:
        row = session.get(Instance, instance["id"])
        row.guest_os = GuestOS.WINDOWS
        session.add(row)
        session.commit()

    windows = vol_client.get(f"/volumes/{volume['id']}").json()
    assert windows["attached_instance_guest_os"] == "windows"
    # Both names stay on the payload either way — the field says which to use,
    # so nothing has to be recomputed or withheld.
    assert windows["device_hint"] == "vdb"
    assert windows["windows_disk_hint"] == "Disk 1"


def test_a_detached_volume_claims_no_guest_and_no_device(vol_client):
    """It has no guest yet, so guessing what one would call it is inventing."""
    volume = _volume(vol_client, "spare")

    assert volume["attached_instance_guest_os"] is None
    assert volume["device_hint"] is None
    assert volume["windows_disk_hint"] is None


def test_order_survives_a_stop_start_cycle(vol_client):
    """The runtime file is what a restart replays, so the order must be in it."""
    instance = _instance(vol_client)
    for name in ("first", "second"):
        _attach(vol_client, _volume(vol_client, name)["id"], instance["id"])

    vol_client.post(f"/instances/{instance['id']}/start")
    vol_client.post(f"/instances/{instance['id']}/stop")

    order = [
        v["name"]
        for v in sorted(
            vol_client.get("/volumes", params={"instance_id": instance["id"]}).json(),
            key=lambda v: v["attach_order"],
        )
    ]
    assert order == ["first", "second"]


def test_detaching_the_first_volume_renumbers_the_rest(vol_client):
    """Otherwise the surviving volume would claim vdc while the guest, given
    one drive argument, calls it vdb."""
    instance = _instance(vol_client)
    first = _volume(vol_client, "first")
    second = _volume(vol_client, "second")
    _attach(vol_client, first["id"], instance["id"])
    _attach(vol_client, second["id"], instance["id"])

    vol_client.post(f"/volumes/{first['id']}/detach")

    remaining = vol_client.get(f"/volumes/{second['id']}").json()
    assert remaining["attach_order"] == 0
    assert remaining["device_hint"] == "vdb"


def test_the_engine_is_told_the_new_order(vol_client):
    """The database decides; the engine's runtime file has to agree with it."""
    instance = _instance(vol_client)
    volume = _volume(vol_client, "only")
    _attach(vol_client, volume["id"], instance["id"])

    assert vol_client.fake.volumes.get("web-01") == [
        vol_client.get(f"/volumes/{volume['id']}").json()["path"]
    ]

    vol_client.post(f"/volumes/{volume['id']}/detach")
    assert vol_client.fake.volumes.get("web-01") == []


# --------------------------------------------------------------------------- #
# The stopped-only rule
# --------------------------------------------------------------------------- #
def test_attaching_to_a_running_instance_is_refused_with_the_reason(vol_client):
    """"Stop it first" invites "why?", and the answer is not something the user
    did wrong — it is what this hypervisor's hot-unplug does to a mounted
    filesystem."""
    volume = _volume(vol_client)
    r = vol_client.post("/instances", json={"name": "running-one"})
    running = r.json()

    result = _attach(vol_client, volume["id"], running["id"])

    assert result.status_code == 409
    detail = result.json()["detail"]
    assert "without waiting for the guest" in detail
    assert "corrupts a mounted filesystem" in detail


def test_detaching_from_a_running_instance_is_refused(vol_client):
    volume = _volume(vol_client)
    instance = _instance(vol_client)
    _attach(vol_client, volume["id"], instance["id"])
    vol_client.post(f"/instances/{instance['id']}/start")

    r = vol_client.post(f"/volumes/{volume['id']}/detach")

    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# One writer at a time
# --------------------------------------------------------------------------- #
def test_a_volume_cannot_be_attached_to_two_instances(vol_client):
    """Two guests writing one filesystem corrupts it."""
    volume = _volume(vol_client)
    first = _instance(vol_client, "web-01")
    second = _instance(vol_client, "web-02")
    _attach(vol_client, volume["id"], first["id"])

    r = _attach(vol_client, volume["id"], second["id"])

    assert r.status_code == 409
    assert "already attached to 'web-01'" in r.json()["detail"]


def test_a_volume_still_creating_cannot_be_attached(vol_client):
    volume = _volume(vol_client)
    instance = _instance(vol_client)
    with Session(vol_client.db_engine) as session:
        row = session.get(Volume, volume["id"])
        row.status = VolumeStatus.CREATING
        session.add(row)
        session.commit()

    r = _attach(vol_client, volume["id"], instance["id"])

    assert r.status_code == 409
    assert "not Available" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Deletion — the only operation that destroys data
# --------------------------------------------------------------------------- #
def test_deleting_an_attached_volume_is_refused(vol_client):
    volume = _volume(vol_client)
    instance = _instance(vol_client)
    _attach(vol_client, volume["id"], instance["id"])

    r = vol_client.delete(f"/volumes/{volume['id']}")

    assert r.status_code == 409
    assert "Detach it first" in r.json()["detail"]


def test_deleting_a_detached_volume_removes_the_file(vol_client):
    volume = _volume(vol_client)
    path = Path(vol_client.get(f"/volumes/{volume['id']}").json()["path"])
    assert path.exists()

    assert vol_client.delete(f"/volumes/{volume['id']}").status_code == 204

    assert not path.exists()
    assert vol_client.get(f"/volumes/{volume['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def test_a_created_volume_becomes_available_with_a_real_file(vol_client):
    volume = _volume(vol_client, "data", 2)

    assert volume["status"] == "Available"
    assert volume["size_gb"] == 2
    assert Path(volume["path"]).exists()


def test_duplicate_names_are_refused_within_a_project(vol_client):
    _volume(vol_client, "data")

    r = vol_client.post("/volumes", json={"name": "data", "size_gb": 1})

    assert r.status_code == 409


def test_the_same_name_is_allowed_in_another_project(vol_client):
    """Unlike an instance name, a volume name is not the hypervisor's identity
    — the file is named by uuid — so it can be per-project (DECISIONS #22)."""
    other = vol_client.post("/projects", json={"name": "client-a"}).json()
    _volume(vol_client, "data")

    r = vol_client.post(
        "/volumes", json={"name": "data", "size_gb": 1, "project_id": other["id"]}
    )

    assert r.status_code == 202


def test_a_volume_larger_than_free_disk_is_refused_with_the_arithmetic(vol_client):
    r = vol_client.post("/volumes", json={"name": "huge", "size_gb": 100_000})

    assert r.status_code == 422
    assert "only" in r.json()["detail"] and "free" in r.json()["detail"]


def test_a_zero_or_negative_size_is_rejected(vol_client):
    assert vol_client.post("/volumes", json={"name": "x", "size_gb": 0}).status_code == 422
    assert vol_client.post("/volumes", json={"name": "x", "size_gb": -5}).status_code == 422


def test_volumes_filter_by_project_and_instance(vol_client):
    instance = _instance(vol_client)
    attached = _volume(vol_client, "attached")
    _volume(vol_client, "loose")
    _attach(vol_client, attached["id"], instance["id"])

    by_instance = vol_client.get("/volumes", params={"instance_id": instance["id"]}).json()

    assert [v["name"] for v in by_instance] == ["attached"]


def test_an_unknown_volume_is_a_404(vol_client):
    assert vol_client.get("/volumes/nope").status_code == 404
    assert vol_client.post("/volumes/nope/detach").status_code == 404
