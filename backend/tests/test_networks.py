"""
Networks and port forwards.

Three things are asserted hardest here.

**Collisions are refused with the reason named.** A host port can be taken four
ways — this instance's SSH port, another instance's pinned port, an existing
forward, or a pool this system allocates from — and "port in use" without
saying which sends someone to netstat for something the API already knows.

**The SSH forward is derived, listed, and undeletable.** It lives on
``Instance.ssh_port``, not in the forwards table (DECISIONS #25), because every
instance since Phase 5 depends on it. It still appears in the listing, marked
``derived``, so the answer to "what reaches this guest" is complete.

**Deferred modes explain themselves.** The API states what bridged and
host-only would require of the operator, measured rather than guessed, so the
UI can show a reason instead of a disabled control.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from app.models import Instance, InstanceStatus, Network, PortForward

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures


def _instance(client, name: str = "web-01") -> dict:
    r = client.post("/instances", json={"name": name})
    assert r.status_code == 202, r.text
    return client.get(f"/instances/{r.json()['id']}").json()


def _forwards(client, instance_id: str) -> list[dict]:
    r = client.get(f"/instances/{instance_id}/forwards")
    assert r.status_code == 200, r.text
    return r.json()


def _add(client, instance_id: str, host: int, guest: int, **extra):
    return client.post(
        f"/instances/{instance_id}/forwards",
        json={"host_port": host, "guest_port": guest, **extra},
    )


# --------------------------------------------------------------------------- #
# The network model
# --------------------------------------------------------------------------- #
def test_a_default_user_network_exists_without_anyone_creating_one(client):
    networks = client.get("/networks").json()

    assert len(networks) == 1
    assert networks[0]["mode"] == "user"
    assert networks[0]["is_default"] is True


def test_an_instance_lands_on_the_default_network(client):
    """Existing instances map onto the model rather than having no network."""
    instance = _instance(client)

    assert instance["network_id"] == client.get("/networks").json()[0]["id"]


def test_an_unknown_network_is_a_422(client):
    r = client.post("/instances", json={"name": "web-01", "network_id": "nope"})

    assert r.status_code == 422
    assert "Unknown network" in r.json()["detail"]


def test_deferred_modes_say_what_they_would_require(client):
    """Genuinely useful information about the operator's machine, not an
    apology for a missing feature."""
    modes = client.get("/networks/modes").json()

    assert [m["mode"] for m in modes["available"]] == ["user"]
    deferred = {m["mode"]: m for m in modes["deferred"]}
    assert set(deferred) == {"host_only", "bridged"}

    # The elevation each needs, named per platform.
    assert "Administrator" in deferred["bridged"]["requires"]
    assert "tap-windows6" in deferred["bridged"]["requires"]
    assert "CAP_NET_ADMIN" in deferred["bridged"]["requires"]
    # And why host-only is not merely unimplemented.
    assert "multicast" in deferred["host_only"]["blocker"]


# --------------------------------------------------------------------------- #
# The SSH forward: derived, listed, undeletable
# --------------------------------------------------------------------------- #
def test_the_ssh_forward_is_listed_and_marked_derived(client):
    instance = _instance(client)

    forwards = _forwards(client, instance["id"])

    ssh = forwards[0]
    assert ssh["derived"] is True
    assert ssh["guest_port"] == 22
    assert ssh["host_port"] == instance["ssh_port"]
    assert ssh["id"] == "ssh"


def test_the_ssh_forward_cannot_be_deleted(client):
    """A control that appears to work and then brings the row back is worse
    than no control, so the refusal is explicit and explains itself."""
    instance = _instance(client)

    r = client.delete(f"/instances/{instance['id']}/forwards/ssh")

    assert r.status_code == 409
    assert "cannot be removed" in r.json()["detail"]
    assert "Terminate the instance" in r.json()["detail"]


def test_the_ssh_forward_is_not_a_row_in_the_table(client):
    """The whole point of special-casing it: no migration, no backfill, no way
    for a missing row to produce an unreachable instance."""
    instance = _instance(client)
    _add(client, instance["id"], 18080, 80)

    with Session(client.db_engine) as session:
        rows = session.exec(select(PortForward)).all()

    assert [(r.host_port, r.guest_port) for r in rows] == [(18080, 80)]


# --------------------------------------------------------------------------- #
# Collisions — each names what is using the port
# --------------------------------------------------------------------------- #
def test_forwarding_this_instances_own_ssh_port_is_refused(client):
    instance = _instance(client)

    r = _add(client, instance["id"], instance["ssh_port"], 80)

    assert r.status_code == 422
    assert "this instance's SSH port" in r.json()["detail"]


def test_forwarding_another_instances_pinned_port_names_that_instance(client):
    first = _instance(client, "web-01")
    second = _instance(client, "web-02")

    # The fake hands every instance the same pinned ports; give the second one
    # its own so the collision under test is "another instance holds it"
    # rather than "this is your own SSH port", which is a different message.
    with Session(client.db_engine) as session:
        row = session.get(Instance, second["id"])
        row.ssh_port = 2299
        session.add(row)
        session.commit()

    r = _add(client, second["id"], first["ssh_port"], 80)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "web-01" in detail and "SSH port" in detail
    assert "will not free up" in detail


def test_a_port_already_forwarded_names_the_instance_holding_it(client):
    first = _instance(client, "web-01")
    second = _instance(client, "web-02")
    assert _add(client, first["id"], 18080, 80).status_code == 201

    r = _add(client, second["id"], 18080, 8080)

    assert r.status_code == 422
    assert "already forwards to instance 'web-01'" in r.json()["detail"]


def test_a_port_inside_an_allocation_pool_is_refused_with_the_range(client):
    """Not in use *yet*, but it will be handed to a future launch — and a
    launch failing because a forward squatted on its port is a confusing way
    to discover that."""
    instance = _instance(client)

    r = _add(client, instance["id"], 2250, 80)  # inside the SSH pool

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "SSH port pool" in detail and "2200-2299" in detail
    assert "some future launch fail" in detail


def test_the_same_host_port_on_two_instances_is_the_conflict_not_the_guest_port(client):
    """Two guests can both expose port 80; only the host side must be unique."""
    first = _instance(client, "web-01")
    second = _instance(client, "web-02")

    assert _add(client, first["id"], 18080, 80).status_code == 201
    assert _add(client, second["id"], 18081, 80).status_code == 201


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_a_port_outside_the_valid_range_is_rejected(client, port):
    instance = _instance(client)
    assert _add(client, instance["id"], port, 80).status_code == 422


# --------------------------------------------------------------------------- #
# Applying forwards
# --------------------------------------------------------------------------- #
def test_adding_a_forward_reaches_the_engine(client):
    instance = _instance(client)

    _add(client, instance["id"], 18080, 80)

    assert "tcp:127.0.0.1:18080-:80" in client.fake.forwards.get("web-01", [])


def test_removing_a_forward_reaches_the_engine_and_drops_the_row(client):
    instance = _instance(client)
    forward = _add(client, instance["id"], 18080, 80).json()

    r = client.delete(f"/instances/{instance['id']}/forwards/{forward['id']}")

    assert r.status_code == 204
    assert client.fake.forwards.get("web-01") == []
    assert len(_forwards(client, instance["id"])) == 1  # the SSH one remains


def test_a_forward_the_engine_refuses_is_not_recorded(client):
    """Otherwise the database would claim a forward that does not exist."""
    from app.engines import ComputeEngineError

    instance = _instance(client)

    def _boom(name, spec):  # noqa: ANN001
        raise ComputeEngineError("QEMU refused the port forward")

    client.fake.add_port_forward = _boom
    r = _add(client, instance["id"], 18080, 80)

    assert r.status_code == 502
    assert len(_forwards(client, instance["id"])) == 1  # SSH only


def test_forwards_are_recorded_in_the_event_log(client):
    instance = _instance(client)
    forward = _add(client, instance["id"], 18080, 80).json()
    client.delete(f"/instances/{instance['id']}/forwards/{forward['id']}")

    kinds = [e["kind"] for e in client.get(f"/instances/{instance['id']}/events").json()]

    assert kinds[:2] == ["port_forward_removed", "port_forward_added"]


def test_terminating_an_instance_drops_its_forwards(client):
    """The VM's SLIRP stack is gone, so the ports are already free; keeping the
    rows would advertise forwards into nothing."""
    instance = _instance(client)
    _add(client, instance["id"], 18080, 80)

    client.delete(f"/instances/{instance['id']}")

    with Session(client.db_engine) as session:
        assert session.exec(select(PortForward)).all() == []


def test_a_port_freed_by_a_terminate_can_be_forwarded_again(client):
    first = _instance(client, "web-01")
    _add(client, first["id"], 18080, 80)
    client.delete(f"/instances/{first['id']}")

    second = _instance(client, "web-02")
    assert _add(client, second["id"], 18080, 80).status_code == 201


def test_forwarding_to_a_terminated_instance_is_refused(client):
    instance = _instance(client)
    client.delete(f"/instances/{instance['id']}")

    r = _add(client, instance["id"], 18080, 80)

    assert r.status_code == 409
    assert "nothing to forward to" in r.json()["detail"]


def test_an_unknown_forward_is_a_404(client):
    instance = _instance(client)
    assert client.delete(f"/instances/{instance['id']}/forwards/nope").status_code == 404


# --------------------------------------------------------------------------- #
# Forward presets (Phase 13)
# --------------------------------------------------------------------------- #
def test_the_rdp_preset_is_offered_for_windows(client):
    r = client.get("/forwards/presets", params={"guest_os": "windows"})

    assert r.status_code == 200, r.text
    presets = r.json()
    rdp = next(p for p in presets if p["key"] == "rdp")
    assert rdp["guest_port"] == 3389
    assert rdp["protocol"] == "tcp"
    # Says the part that is not obvious: the forward alone does not turn RDP on.
    assert "off by default" in rdp["description"]


def test_windows_only_presets_are_not_offered_to_linux(client):
    """Offering RDP for a Linux guest would be presenting an invalid
    combination, which is the rule the audit set for every other control."""
    keys = [p["key"] for p in client.get(
        "/forwards/presets", params={"guest_os": "linux"}
    ).json()]

    assert "rdp" not in keys


def test_presets_do_not_create_anything(client, iso_dir):  # noqa: F811
    """A preset fills the form in. The ordinary create endpoint still does the
    work, so there is one collision check and one code path."""
    (iso_dir / "server.iso").write_bytes(b"\0" * 64)
    instance = client.post("/instances", json={
        "name": "win-rdp", "guest_os": "windows",
        "flavor": "windows", "iso": "server.iso",
    }).json()

    client.get("/forwards/presets", params={"guest_os": "windows"})
    forwards = client.get(f"/instances/{instance['id']}/forwards").json()

    assert all(f["guest_port"] != 3389 for f in forwards)
