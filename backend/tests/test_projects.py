"""
Projects: grouping, and the several ways that could be got wrong.

The load-bearing assertions here are the ones about what a project is *not*.
It is not an isolation boundary, so nothing may be hidden from a caller who
does not filter; and it is not a container, so deleting one must never destroy
what was filed under it.

The rest is migration safety — an existing install must not notice this feature
exists beyond gaining a "default" label — and the naming decision from
DECISIONS #20: instance names stay globally unique, because the name is the
hypervisor's identity on disk.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from kurukuru.models import Image, ImageStatus, Instance, InstanceStatus, KeyPair, Project

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures

# The same throwaway key the key-pair tests use; see test_keypairs.py for why
# it is a real one. Nothing here checks a fingerprint — these tests only need a
# key that parses — but keeping the two in step means there is one sample key
# in the suite rather than two that could diverge.
SAMPLE_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFsNa9bJGdWjcUKzG2RG6/CcJIsS/bFe7P0L"
    "r2K3Fl5y sample@local-iaas.test"
)


def _projects(client) -> list[dict]:
    r = client.get("/projects")
    assert r.status_code == 200, r.text
    return r.json()


def _default(client) -> dict:
    return next(p for p in _projects(client) if p["is_default"])


def _create(client, name: str, **extra) -> dict:
    r = client.post("/projects", json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _launch(client, name: str, **extra) -> dict:
    r = client.post("/instances", json={"name": name, **extra})
    assert r.status_code == 202, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# The default project
# --------------------------------------------------------------------------- #
def test_a_default_project_exists_without_anyone_creating_one(client):
    projects = _projects(client)

    assert len(projects) == 1
    assert projects[0]["is_default"] is True
    assert projects[0]["name"] == "default"


def test_an_instance_launched_without_a_project_lands_in_the_default(client):
    """The behaviour of every launch before this feature existed."""
    instance = _launch(client, "web-01")

    assert instance["project_id"] == _default(client)["id"]


def test_the_default_project_cannot_be_deleted(client):
    """Deleting it would leave nowhere to move another project's resources."""
    r = client.delete(f"/projects/{_default(client)['id']}")

    assert r.status_code == 409
    assert "cannot be deleted" in r.json()["detail"]


def test_the_default_is_listed_first(client):
    _create(client, "aardvark")

    assert _projects(client)[0]["is_default"] is True


# --------------------------------------------------------------------------- #
# Filtering — a view, never a permission
# --------------------------------------------------------------------------- #
def test_filtering_narrows_the_list(client):
    other = _create(client, "client-a")
    _launch(client, "web-01")
    _launch(client, "web-02", project_id=other["id"])

    everything = client.get("/instances").json()
    filtered = client.get("/instances", params={"project_id": other["id"]}).json()

    assert {i["name"] for i in everything} == {"web-01", "web-02"}
    assert {i["name"] for i in filtered} == {"web-02"}


def test_omitting_the_filter_shows_every_project(client):
    """A project is not a boundary: nothing is hidden from a caller who does
    not ask for a subset."""
    other = _create(client, "client-a")
    _launch(client, "web-01")
    _launch(client, "web-02", project_id=other["id"])

    assert len(client.get("/instances").json()) == 2


def test_an_instance_in_another_project_is_still_readable_and_actionable(client):
    """The honest consequence of "not a security boundary", asserted so nobody
    later mistakes the filter for an access control."""
    other = _create(client, "client-a")
    instance = _launch(client, "web-01", project_id=other["id"])

    assert client.get(f"/instances/{instance['id']}").status_code == 200
    assert client.post(f"/instances/{instance['id']}/stop").status_code == 200


def test_an_unknown_project_on_create_is_a_422(client):
    r = client.post("/instances", json={"name": "web-01", "project_id": "nope"})

    assert r.status_code == 422
    assert "Unknown project" in r.json()["detail"]


def test_images_and_keypairs_filter_too(client):
    other = _create(client, "client-a")
    client.post("/keypairs/import", json={"name": "mine", "public_key": SAMPLE_KEY,
                                          "project_id": other["id"]})

    filtered = client.get("/keypairs", params={"project_id": other["id"]}).json()

    assert "mine" in {k["name"] for k in filtered}


def test_the_orchestrator_key_survives_every_filter(client):
    """The one exemption, and it earns it: it is the default key for a launch
    in every project, so a filtered view that hid it would offer a launch with
    no way into the guest."""
    other = _create(client, "client-a")

    filtered = client.get("/keypairs", params={"project_id": other["id"]}).json()

    assert "orchestrator" in {k["name"] for k in filtered}


# --------------------------------------------------------------------------- #
# Names stay global (DECISIONS #20)
# --------------------------------------------------------------------------- #
def test_two_projects_cannot_hold_an_instance_with_the_same_name(client):
    """The name is the hypervisor's identity — the on-disk directory, the QEMU
    process label, the cloud-init instance-id. Two 'web' instances would share
    one disk.qcow2 and one set of pinned ports."""
    other = _create(client, "client-a")
    _launch(client, "web-01")

    r = client.post("/instances", json={"name": "web-01", "project_id": other["id"]})

    assert r.status_code == 409
    assert "unique across all projects" in r.json()["detail"]


def test_the_conflict_names_the_project_holding_the_name(client):
    """Only safe to say because a project is not a security boundary. If it
    ever became one, this line would be a leak."""
    other = _create(client, "client-a")
    _launch(client, "web-01", project_id=other["id"])

    r = client.post("/instances", json={"name": "web-01"})

    assert "client-a" in r.json()["detail"]


def test_a_terminated_name_is_reusable_in_any_project(client):
    """Uniqueness is over live rows, as it always was — projects do not change
    that."""
    other = _create(client, "client-a")
    instance = _launch(client, "web-01")
    client.delete(f"/instances/{instance['id']}")

    r = client.post("/instances", json={"name": "web-01", "project_id": other["id"]})

    assert r.status_code == 202


# --------------------------------------------------------------------------- #
# Deletion moves, never destroys
# --------------------------------------------------------------------------- #
def test_deleting_a_project_holding_live_instances_is_refused_by_name(client):
    """Tidying the sidebar must not silently re-file a running VM."""
    other = _create(client, "client-a")
    _launch(client, "web-01", project_id=other["id"])
    _launch(client, "web-02", project_id=other["id"])

    r = client.delete(f"/projects/{other['id']}")

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "web-01" in detail and "web-02" in detail


def test_terminated_instances_do_not_block_deletion(client):
    """They are audit history. A project full of destroyed VMs would otherwise
    be undeletable forever."""
    other = _create(client, "client-a")
    instance = _launch(client, "web-01", project_id=other["id"])
    client.delete(f"/instances/{instance['id']}")

    assert client.delete(f"/projects/{other['id']}").status_code == 204


def test_deleting_a_project_moves_its_images_and_keys_to_the_default(client):
    """Deleting a 4 GB image because someone removed a label it carried would
    be indefensible."""
    other = _create(client, "client-a")
    keypair = client.post(
        "/keypairs/import",
        json={"name": "mine", "public_key": SAMPLE_KEY, "project_id": other["id"]},
    ).json()

    assert client.delete(f"/projects/{other['id']}").status_code == 204

    survivor = client.get(f"/keypairs/{keypair['id']}")
    assert survivor.status_code == 200
    assert survivor.json()["project_id"] == _default(client)["id"]


def test_a_terminated_instances_history_moves_rather_than_vanishing(client):
    """Its row is the only record the instance ever existed."""
    other = _create(client, "client-a")
    instance = _launch(client, "web-01", project_id=other["id"])
    client.delete(f"/instances/{instance['id']}")

    client.delete(f"/projects/{other['id']}")

    row = client.get(f"/instances/{instance['id']}")
    assert row.status_code == 200
    assert row.json()["project_id"] == _default(client)["id"]


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_duplicate_project_names_are_refused(client):
    _create(client, "client-a")

    assert client.post("/projects", json={"name": "client-a"}).status_code == 409


def test_a_project_can_be_renamed(client):
    project = _create(client, "clietn-a")  # the typo people actually make

    r = client.patch(f"/projects/{project['id']}", json={"name": "client-a"})

    assert r.status_code == 200
    assert r.json()["name"] == "client-a"


def test_renaming_onto_an_existing_name_is_refused(client):
    _create(client, "client-a")
    second = _create(client, "client-b")

    r = client.patch(f"/projects/{second['id']}", json={"name": "client-a"})

    assert r.status_code == 409


def test_counts_report_what_is_filed_under_a_project(client):
    other = _create(client, "client-a")
    _launch(client, "web-01", project_id=other["id"])

    counts = client.get(f"/projects/{other['id']}").json()

    assert counts["instance_count"] == 1
    assert counts["image_count"] == 0


def test_counts_ignore_terminated_instances(client):
    """Or a project would look full of VMs that no longer exist."""
    other = _create(client, "client-a")
    instance = _launch(client, "web-01", project_id=other["id"])
    client.delete(f"/instances/{instance['id']}")

    assert client.get(f"/projects/{other['id']}").json()["instance_count"] == 0


def test_an_unknown_project_is_a_404(client):
    assert client.get("/projects/nope").status_code == 404


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "   "}])
def test_a_blank_name_is_refused(client, body):
    assert client.post("/projects", json=body).status_code == 422
