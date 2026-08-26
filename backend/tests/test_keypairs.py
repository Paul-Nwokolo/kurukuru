"""
Key pair catalog: parsing, fingerprints, and what lands in a guest.

The fingerprint tests use a fixed key with a fingerprint verified against
``ssh-keygen -lf``, so they assert OpenSSH's answer rather than ours. The rest
is about the two things that could hurt someone: a key that silently fails to
reach the guest, and the orchestrator key going missing from instances that
depend on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.keypairs import KeyPairError, generate_keypair, parse_public_key
from app.models import (
    Instance,
    InstanceKeyPair,
    InstanceStatus,
    KeyPair,
    KeyPairSource,
)

from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures

# A real ed25519 public key, and it has to be real: the fingerprint below was
# taken from `ssh-keygen -lf` on this exact key, which is what pins our
# arithmetic to OpenSSH's rather than to itself. A made-up blob would let both
# sides of the comparison drift together and the test would still pass.
#
# It is also a throwaway, generated for the suite and belonging to nobody — the
# private half was deleted the moment the public half was pasted here. It used
# to be a developer's own key, which put a real name and machine name in a repo
# headed for open source for no test-related reason at all.
SAMPLE_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFsNa9bJGdWjcUKzG2RG6/CcJIsS/bFe7P0L"
    "r2K3Fl5y sample@local-iaas.test"
)
SAMPLE_FINGERPRINT = "SHA256:oQrznHDNeWU9WDpn8oJqtSrBNtPVbsbbRFo5wvL2DOs"


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_fingerprint_matches_openssh():
    """The definition of a fingerprint is whatever ssh-keygen prints."""
    parsed = parse_public_key(SAMPLE_KEY)

    assert parsed.fingerprint == SAMPLE_FINGERPRINT
    assert parsed.key_type == "ssh-ed25519"
    assert parsed.label == "ed25519"
    assert parsed.comment == "sample@local-iaas.test"


def test_whitespace_and_newlines_are_tolerated():
    """People paste these by hand, often out of a terminal that wrapped them."""
    messy = f"  \n {SAMPLE_KEY}\n\n"
    assert parse_public_key(messy).fingerprint == SAMPLE_FINGERPRINT


def test_a_body_that_belongs_to_another_algorithm_is_rejected():
    """The structural check, and the only thing that catches this.

    The base64 decodes perfectly and the line looks well-formed; only reading
    the type embedded in the blob reveals that it is not an RSA key.
    """
    body = SAMPLE_KEY.split()[1]
    with pytest.raises(KeyPairError, match="declares 'ssh-ed25519'"):
        parse_public_key(f"ssh-rsa {body}")


def test_a_private_key_is_refused_by_name():
    """The likeliest paste mistake, and the most important to get right."""
    with pytest.raises(KeyPairError, match="private"):
        parse_public_key("-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef\n")


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "No key"),
        ("ssh-ed25519", "type and a body"),
        ("ssh-ed25519 !!!not-base64!!!", "base64"),
        ("ssh-ed25519 AAAA", "truncated"),
        ("ssh-dss AAAAB3NzaC1kc3M=", "Unsupported key type"),
    ],
)
def test_malformed_keys_are_refused_with_the_actual_reason(text, match):
    with pytest.raises(KeyPairError, match=match):
        parse_public_key(text)


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def test_generate_writes_a_usable_pair(tmp_path: Path):
    from app.config import Settings

    settings = Settings(ssh_key_dir=str(tmp_path / "keys"))
    private, public = generate_keypair("My Laptop Key", settings)

    assert private.exists() and private.with_suffix(".pub").exists()
    assert parse_public_key(public).label == "ed25519"
    # The name is slugged into the filename, never used as one: it is free text
    # and this is a path.
    assert private.name.startswith("my-laptop-key-")


@pytest.mark.posix
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_generated_private_keys_are_0600(tmp_path: Path):
    """OpenSSH refuses a private key others can read, so this is load-bearing."""
    from app.config import Settings

    private, _ = generate_keypair("perm-check", Settings(ssh_key_dir=str(tmp_path / "k")))
    assert oct(private.stat().st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #
def test_orchestrator_key_is_adopted_not_recreated(client):
    """It is seeded from the existing key file; instances already trust it."""
    rows = client.get("/keypairs").json()
    orchestrator = [k for k in rows if k["source"] == "orchestrator"]

    assert len(orchestrator) == 1
    assert orchestrator[0]["has_private_key"] is True
    assert orchestrator[0]["private_key_path"]


def test_seeding_is_idempotent(client):
    """Startup runs on every boot; it must not accumulate rows."""
    from app.routers.keypairs import ensure_orchestrator_keypair

    ensure_orchestrator_keypair()
    ensure_orchestrator_keypair()

    rows = client.get("/keypairs").json()
    assert len([k for k in rows if k["source"] == "orchestrator"]) == 1


def test_import_stores_the_key_and_its_fingerprint(client):
    r = client.post("/keypairs/import", json={"name": "laptop", "public_key": SAMPLE_KEY})

    assert r.status_code == 201
    body = r.json()
    assert body["fingerprint"] == SAMPLE_FINGERPRINT
    assert body["source"] == "imported"
    assert body["has_private_key"] is False
    assert body["private_key_path"] is None


def test_import_rejects_a_bad_key_with_a_422_naming_the_problem(client):
    r = client.post("/keypairs/import", json={"name": "bad", "public_key": "nonsense"})

    assert r.status_code == 422
    assert "type and a body" in r.json()["detail"] or "Unsupported" in r.json()["detail"]


def test_duplicate_names_are_refused(client):
    client.post("/keypairs/import", json={"name": "dupe", "public_key": SAMPLE_KEY})
    r = client.post("/keypairs/import", json={"name": "dupe", "public_key": SAMPLE_KEY})
    assert r.status_code == 409


def test_generate_never_returns_the_private_key(client):
    """There is no endpoint that hands out a private key, at creation or after."""
    r = client.post("/keypairs/generate", json={"name": "fresh"})

    assert r.status_code == 201
    body = r.json()
    assert body["private_key_path"]           # where it is
    assert "private_key" not in body          # not what it is
    assert "BEGIN" not in str(body)

    again = client.get(f"/keypairs/{body['id']}").json()
    assert "private_key" not in again


def test_generate_writes_into_the_configured_key_directory(client, tmp_path):
    """Not the real one.

    ``/keypairs/generate`` runs ssh-keygen and writes two files, and for ten
    runs it wrote them into the developer's own ``~/.kurukuru/keys`` — the
    test settings pinned the ISO directory but left the key directory at its
    default. Nothing failed; the orphans just accumulated. This asserts the
    thing that was silently untrue.
    """
    body = client.post("/keypairs/generate", json={"name": "somewhere-else"}).json()

    assert Path(body["private_key_path"]).is_relative_to(tmp_path)


def test_the_orchestrator_key_cannot_be_deleted(client):
    """Every instance in the field trusts it."""
    orchestrator = [k for k in client.get("/keypairs").json() if k["source"] == "orchestrator"][0]

    r = client.delete(f"/keypairs/{orchestrator['id']}")
    assert r.status_code == 409
    assert "cannot be deleted" in r.json()["detail"]


def test_other_keys_can_be_deleted(client):
    created = client.post(
        "/keypairs/import", json={"name": "temp", "public_key": SAMPLE_KEY}
    ).json()

    assert client.delete(f"/keypairs/{created['id']}").status_code == 204
    assert client.get(f"/keypairs/{created['id']}").status_code == 404


# --------------------------------------------------------------------------- #
# What actually reaches the guest
# --------------------------------------------------------------------------- #
def _seeded_keys(client, name: str) -> list[str]:
    """The ssh_authorized_keys the provisioning job rendered for an instance."""
    from app.cloud_init import build_config
    from app.routers.instances import instance_public_keys

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        instance = session.exec(select(Instance).where(Instance.name == name)).one()
        keys = instance_public_keys(session, instance)
    return build_config(name, public_keys=keys)["users"][0]["ssh_authorized_keys"]


def test_omitting_keypairs_installs_the_orchestrator_key_exactly_as_before(client):
    """The default has to be indistinguishable from the pre-keypair behaviour."""
    client.post("/instances", json={"name": "legacy-default", "flavor": "small"})

    keys = _seeded_keys(client, "legacy-default")
    orchestrator = [k for k in client.get("/keypairs").json() if k["source"] == "orchestrator"][0]
    assert keys == [orchestrator["public_key"]]


def test_selected_keypairs_all_reach_the_guest(client):
    imported = client.post(
        "/keypairs/import", json={"name": "second", "public_key": SAMPLE_KEY}
    ).json()
    orchestrator = [k for k in client.get("/keypairs").json() if k["source"] == "orchestrator"][0]

    client.post(
        "/instances",
        json={
            "name": "two-keys",
            "flavor": "small",
            "keypair_ids": [orchestrator["id"], imported["id"]],
        },
    )

    keys = _seeded_keys(client, "two-keys")
    assert set(keys) == {orchestrator["public_key"], imported["public_key"]}


def test_an_empty_keypair_list_means_no_keys(client):
    """A definite request for a console-only instance, honoured as given."""
    client.post("/instances", json={"name": "no-keys", "flavor": "small", "keypair_ids": []})

    assert _seeded_keys(client, "no-keys") == []


def test_an_unknown_keypair_is_a_422_on_the_request(client):
    """Not an Error row seconds later — the same rule as images and ISOs."""
    r = client.post(
        "/instances",
        json={"name": "ghost-key", "flavor": "small", "keypair_ids": ["no-such-id"]},
    )

    assert r.status_code == 422
    assert "Unknown key pair" in r.json()["detail"]


def test_losing_some_keys_before_provisioning_still_installs_the_rest(client):
    """Fewer keys than intended beats no instance at all."""
    doomed = client.post(
        "/keypairs/import", json={"name": "doomed-one", "public_key": SAMPLE_KEY}
    ).json()
    orchestrator = [k for k in client.get("/keypairs").json() if k["source"] == "orchestrator"][0]

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        instance = Instance(name="partial-keys", status=InstanceStatus.PENDING)
        session.add(instance)
        session.commit()
        for kp in (orchestrator, doomed):
            session.add(
                InstanceKeyPair(
                    instance_id=instance.id,
                    keypair_id=kp["id"],
                    keypair_name=kp["name"],
                    fingerprint=kp["fingerprint"],
                )
            )
        session.commit()
        instance_id = instance.id

    client.delete(f"/keypairs/{doomed['id']}")

    from app.routers.instances import instance_public_keys

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        keys = instance_public_keys(session, session.get(Instance, instance_id))

    assert keys == [orchestrator["public_key"]]


def test_losing_every_key_before_provisioning_is_an_error_not_a_silent_launch(client):
    """A guest that reports SSH access it cannot honour is worse than no guest.

    The window is small — a keypair deleted between the 202 and the background
    job — but the outcome was a VM that boots with an empty authorized_keys,
    publishes an address, advertises ssh_enabled, and refuses every connection
    made to it. That is the same class of failure as cloud-init not rendering,
    so it gets the same treatment: Error, with a message that says what
    happened.
    """
    imported = client.post(
        "/keypairs/import", json={"name": "only-key", "public_key": SAMPLE_KEY}
    ).json()

    instance = client.post(
        "/instances",
        json={"name": "orphaned", "flavor": "small", "keypair_ids": [imported["id"]]},
    ).json()

    # Provisioning already ran (TestClient runs background tasks inline), so
    # drive the race directly: delete the key, then re-provision the row.
    client.delete(f"/keypairs/{imported['id']}")

    from app.routers.instances import _provision_job

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = session.get(Instance, instance["id"])
        row.status = InstanceStatus.PENDING
        session.add(row)
        session.commit()

    _provision_job(instance["id"])

    row = client.get(f"/instances/{instance['id']}").json()
    assert row["status"] == "Error"
    assert "no way in" in row["error_message"]
    assert "only-key" in row["error_message"]


def test_an_instance_launched_with_no_keys_provisions_normally(client):
    """Console-only is a deliberate choice, not a failure to resolve keys."""
    body = client.post(
        "/instances", json={"name": "console-only", "flavor": "small", "keypair_ids": []}
    )

    assert body.status_code == 202
    row = client.get(f"/instances/{body.json()['id']}").json()
    assert row["status"] == "Running"
    assert row["error_message"] is None


def test_the_association_survives_deleting_the_keypair(client):
    """The key is baked into the guest; deleting our record does not remove it.

    So the instance keeps saying which key was installed, marked as deleted —
    dropping the row would imply the access went away with it.
    """
    imported = client.post(
        "/keypairs/import", json={"name": "doomed", "public_key": SAMPLE_KEY}
    ).json()
    instance = client.post(
        "/instances",
        json={"name": "outlives", "flavor": "small", "keypair_ids": [imported["id"]]},
    ).json()

    client.delete(f"/keypairs/{imported['id']}")

    installed = client.get(f"/instances/{instance['id']}/keypairs").json()
    assert len(installed) == 1
    assert installed[0]["name"] == "doomed"
    assert installed[0]["fingerprint"] == SAMPLE_FINGERPRINT
    assert installed[0]["deleted"] is True
