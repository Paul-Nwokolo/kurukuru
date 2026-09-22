"""Renaming the default VM user without breaking every VM that already exists.

0.1.2 changes ``default_vm_user`` from "iaas" to "kurukuru". That looks like a
string edit and is not: the login is written into a guest's ``/etc/passwd`` at
provision time and can never be changed afterwards from out here, while the
"Copy SSH" command used to be *computed from the setting* every time anybody
looked at a row. So moving the default would have silently rewritten the SSH
command of every existing VM into a username its guest has never heard of. It
fails as "Permission denied (publickey)", which reads as a broken key rather
than a wrong user, and would have sent people looking at their keypairs.

The fix is to make the login a recorded fact rather than a live derivation.
These tests are about the property that fix has to have, stated the way a user
would state it:

    **An instance that existed before the rename must produce exactly the SSH
    command it produced before the rename.**

Everything else here is in support of that one sentence.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

import kurukuru.database as database
from kurukuru.cloud_init import build_config
# Bound at module import, which is *before* conftest's autouse fixture patches
# the name in kurukuru.config. That matters: this exact object is the one
# FastAPI's ``Depends(get_settings)`` captured, so it is the only key an
# override is looked up under. Reaching for ``kurukuru.config.get_settings``
# at call time instead gets the patched replacement, adds a second key nothing
# consults, and silently does nothing — which cost an hour the first time.
from kurukuru.config import Settings, get_settings as _di_get_settings
from kurukuru.database import _PRE_RENAME_VM_USER, _apply_additive_migrations
from kurukuru.models import Instance, InstanceRead, InstanceStatus

from tests.test_instances_api import FAKE_IP, client, anon_client, iso_dir  # noqa: F401


# --------------------------------------------------------------------------- #
# The claim
# --------------------------------------------------------------------------- #
def _ssh_command(instance: dict, key_path: str = "/keys/id_ed25519") -> str:
    """The SSH command a client builds, assembled exactly as the clients do.

    Mirrors ``frontend/src/lib/ssh.ts`` and ``_ssh_command`` in the CLI. Both
    take ``ssh_user``, ``ip_address`` and ``ssh_port`` straight off the API
    response and join them, so reproducing the join here is enough to make
    this a test about what the user pastes rather than about a JSON field.
    """
    return (
        f'ssh -i "{key_path}" -p {instance["ssh_port"]} '
        f'{instance["ssh_user"]}@{instance["ip_address"]}'
    )


def _repoint_settings(client, monkeypatch, **overrides) -> Settings:
    """Run the rest of the test against a differently-configured backend.

    Both the dependency override and the provisioning module's own accessor,
    because the launch path reads the latter directly — a rename applied to
    only one of them would let this test pass on a build where creation and
    display still disagree, which is the bug.
    """
    import kurukuru.routers.instances as instances_module

    current = instances_module.get_settings()
    updated = Settings(
        state_dir=current.state_dir,
        iso_dir=current.iso_dir,
        post_launch_ip_timeout_seconds=current.post_launch_ip_timeout_seconds,
        post_launch_poll_seconds=current.post_launch_poll_seconds,
        **overrides,
    )
    client.app.dependency_overrides[_di_get_settings] = lambda: updated
    # Through monkeypatch so it is undone at the end of the test: this module
    # attribute is the one the provisioning job reads, and a leaked override
    # would silently reconfigure every test that ran afterwards.
    monkeypatch.setattr(instances_module, "get_settings", lambda: updated)
    return updated


def test_an_instance_created_before_the_rename_keeps_its_ssh_command(
    client, monkeypatch
):
    """The whole point, end to end through the API.

    The instance is created through the real launch path while the default is
    still "iaas" — not written into the database by hand, because what is
    being tested is precisely whether the *creation* path records a fact. The
    default then moves to "kurukuru" underneath it, which is exactly what
    upgrading to 0.1.2 does, and the command the dashboard would copy has to
    be byte-for-byte identical afterwards.
    """
    _repoint_settings(client, monkeypatch, default_vm_user=_PRE_RENAME_VM_USER)

    created = client.post(
        "/instances", json={"name": "before-the-rename", "flavor": "small"}
    ).json()
    row = client.get(f"/instances/{created['id']}").json()

    before = _ssh_command(row)
    assert before.endswith("iaas@127.0.0.1"), before

    # The upgrade: the default moves. Nothing else changes.
    _repoint_settings(client, monkeypatch, default_vm_user="kurukuru")

    row_after = client.get(f"/instances/{created['id']}").json()
    after = _ssh_command(row_after)

    assert after == before, (
        "the SSH command for a pre-existing instance changed when the default "
        "VM user was renamed — this is the exact breakage the ssh_user column "
        "exists to prevent"
    )
    assert row_after["ssh_user"] == "iaas"


def test_an_instance_created_after_the_rename_gets_the_new_user(client):
    """The other half: the rename has to actually take effect for new VMs.

    Without this the first test passes trivially on a build that never renamed
    anything.
    """
    created = client.post(
        "/instances", json={"name": "after-the-rename", "flavor": "small"}
    ).json()
    row = client.get(f"/instances/{created['id']}").json()
    assert row["ssh_user"] == "kurukuru"


# --------------------------------------------------------------------------- #
# The mechanism underneath it
# --------------------------------------------------------------------------- #
def test_the_migration_backfills_existing_rows_with_the_old_name(
    tmp_path, monkeypatch
):
    """An upgraded database must end up saying "iaas", not this build's default.

    The trap the backfill is written to avoid: reading
    ``settings.default_vm_user`` would fill every historical row with
    *0.1.2's* value, writing the wrong answer permanently into the column
    whose whole purpose is to hold the right one.
    """
    from sqlalchemy import text
    from sqlmodel import SQLModel, create_engine

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)

    # Age the database back to before the column existed, and put a row in it.
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE "instances" DROP COLUMN "ssh_user"')
        conn.exec_driver_sql(
            "INSERT INTO instances (id, name, flavor, status, engine, boot_source, "
            "guest_os, created_at, updated_at) VALUES ('old', 'legacy-vm', "
            "'small', 'RUNNING', 'qemu', 'IMAGE', 'LINUX', '2026-01-01', "
            "'2026-01-01')"
        )

    _apply_additive_migrations()

    with engine.begin() as conn:
        user = conn.execute(
            text("SELECT ssh_user FROM instances WHERE id = 'old'")
        ).scalar_one()

    assert user == "iaas", (
        "a row that predates the column was backfilled with something other "
        "than the only default that ever shipped"
    )


def test_the_backfill_only_ever_fills_nulls(tmp_path, monkeypatch):
    """Idempotent, and it must not overwrite a login already recorded.

    Migrations run on every start. One that rewrote this column each time
    would undo the rename for new instances on the very next restart.
    """
    from sqlalchemy import text
    from sqlmodel import SQLModel, create_engine

    engine = create_engine(f"sqlite:///{(tmp_path / 'db.db').as_posix()}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)

    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO instances (id, name, flavor, status, engine, boot_source, "
            "guest_os, ssh_user, created_at, updated_at) VALUES ('new', "
            "'new-vm', 'small', 'RUNNING', 'qemu', 'IMAGE', 'LINUX', "
            "'kurukuru', '2026-01-01', '2026-01-01')"
        )

    _apply_additive_migrations()
    _apply_additive_migrations()  # twice: this runs on every start

    with engine.begin() as conn:
        user = conn.execute(
            text("SELECT ssh_user FROM instances WHERE id = 'new'")
        ).scalar_one()

    assert user == "kurukuru"


def test_cloud_init_creates_the_login_the_row_records(client):
    """The guest and the row must agree, which is why cloud-init takes the row.

    Before this change both read ``settings.default_vm_user``, at two different
    moments — provision time and display time. That happened to agree only
    because the setting never changed.
    """
    created = client.post(
        "/instances", json={"name": "agreeing", "flavor": "small"}
    ).json()
    row = client.get(f"/instances/{created['id']}").json()

    config = build_config("agreeing", vm_user=row["ssh_user"], public_keys=["ssh-ed25519 AAAA test"])
    assert config["users"][0]["name"] == row["ssh_user"]


def test_a_clone_inherits_its_sources_login_not_the_current_default(client):
    """A clone's disk is the source's disk, so its accounts are the source's.

    Seeding a clone from the current default would produce an SSH command for
    a user that does not exist in the filesystem that was just copied.
    """
    with Session(client.db_engine) as session:
        session.add(
            Instance(
                name="old-source",
                flavor="small",
                status=InstanceStatus.STOPPED,
                ssh_user="iaas",
                ssh_enabled=True,
                cpus=1,
                memory_mb=1024,
                disk_gb=5,
            )
        )
        session.commit()
        source_id = session.exec(
            select(Instance).where(Instance.name == "old-source")
        ).one().id

    response = client.post(
        f"/instances/{source_id}/clone", json={"name": "the-clone"}
    )
    assert response.status_code in (200, 201, 202), response.text

    clone = client.get(f"/instances/{response.json()['id']}").json()
    assert clone["ssh_user"] == "iaas", (
        "the clone was given this build's default login, but its disk carries "
        "the source's accounts"
    )


def test_a_row_with_no_recorded_login_still_renders_a_usable_command():
    """The defensive fallback, which should never fire in practice.

    A null here would otherwise reach the dashboard as
    ``ssh -p 2200 None@127.0.0.1``.
    """
    row = InstanceRead(
        id="x",
        name="nulluser",
        flavor="small",
        status=InstanceStatus.RUNNING,
        ip_address=FAKE_IP,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        error_message=None,
        ssh_user=None,
    )
    assert row.ssh_user
    assert "None" not in str(row.ssh_user)
