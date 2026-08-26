"""
Router-level tests for the instances API.

These exercise the HTTP contract and state-machine enforcement against a *fake*
ComputeEngine and an in-memory SQLite DB — no QEMU, no network. The FastAPI
TestClient runs BackgroundTasks synchronously after the response, so the async
provisioning path is covered too.

QEMU is the only engine, but the tests still drive it through the registry
rather than reaching for it directly: the per-row dispatch is what a future
driver plugs into, and rows naming the retired Multipass engine still have to be
handled without one.
"""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import app.engines as engines_module
import app.events as events_module
import app.routers.images as images_module
import app.routers.instances as instances_module
import app.routers.keypairs as keypairs_module
import app.routers.snapshots as snapshots_module
from app.config import Settings, get_settings
from app.database import get_session
from app.engines import (
    ComputeEngine,
    ComputeEngineError,
    EngineRegistry,
    InstanceInfo,
    get_engine_registry,
)
from app.main import app
from app.host_capacity import invalidate_cache
from app.models import Image, ImageSource, ImageStatus, Instance, InstanceStatus
from tests.conftest import authenticate_test_client, redirect_db_engines

#: Address the fake hands out, matching QEMU's loopback port-forward model.
FAKE_IP = "127.0.0.1"


class FakeQemuEngine(ComputeEngine):
    """In-memory stand-in for QemuEngine with deterministic behaviour.

    Reports the same shape the real driver does — loopback address plus pinned
    ports — so router assertions about what reaches the API are meaningful.
    """

    def __init__(self, name: str = "qemu") -> None:
        self.name = name
        self.state: dict[str, InstanceInfo] = {}
        self.available = True
        self.calls: list[tuple[str, str]] = []
        # Simulates a hypervisor that reports Running before publishing the
        # guest address: the next N info/list reads withhold the IP.
        self.withhold_ip_reads = 0
        # Simulates an unreachable hypervisor for the next N reads.
        self.fail_reads = 0
        self.read_count = 0
        # ISO guests get no key injection, so the real engine reports them with
        # no address and no SSH port — the console is their only way in.
        self.iso_mode = False
        # Volume snapshot tags, keyed by file path — a volume is
        # identified on disk by path, not by name.
        self._volume_snaps: dict[str, list[str]] = {}
        # What set_volumes was last told, per instance. The real engine writes
        # this into the runtime file; here it is inspectable, which is how the
        # order tests assert the database and the driver agree.
        self.volumes: dict[str, list[str]] = {}
        # What add/remove_port_forward were told, per instance. The real engine
        # writes these into the runtime file and applies them over QMP.
        self.forwards: dict[str, list[str]] = {}
        # (source, target) pairs handed to clone_disk, so a test can assert the
        # clone was flattened from the right instance.
        self.cloned: list[tuple[str, str]] = []

    def _record(self, action: str, name: str) -> None:
        self.calls.append((action, name))

    def is_available(self) -> bool:
        return self.available

    def _read(self, info: InstanceInfo) -> InstanceInfo:
        """Apply the configured read-time faults to one snapshot."""
        self.read_count += 1
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise ComputeEngineError(f"{self.name} is unreachable")
        if self.withhold_ip_reads > 0 and info.exists:
            self.withhold_ip_reads -= 1
            return replace(info, ip_address=None)
        return info

    def _info(self, name: str, status: InstanceStatus, running: bool) -> InstanceInfo:
        return InstanceInfo(
            name=name,
            exists=True,
            status=status,
            ip_address=None if self.iso_mode else (FAKE_IP if running else None),
            ssh_port=None if self.iso_mode else 2222,
            vnc_port=5900,
            qmp_port=4444,
            pid=4242 if running else None,
            # Hardware acceleration for every boot mode — the forced-TCG
            # rule for ISO instances was removed in Phase 7.
            accel="whpx",
            display="std",
        )

    def provision_instance(self, name, cpus, memory, disk, cloud_init_path=None, options=None):
        self._record("provision", name)
        self.state[name] = self._info(name, InstanceStatus.RUNNING, running=True)

    def start_instance(self, name):
        self._record("start", name)
        self.state[name] = self._info(name, InstanceStatus.RUNNING, running=True)

    def stop_instance(self, name):
        self._record("stop", name)
        self.state[name] = self._info(name, InstanceStatus.STOPPED, running=False)

    def destroy_instance(self, name):
        self._record("destroy", name)
        self.state.pop(name, None)

    supports_volumes = True
    supports_port_forwards = True
    supports_clone = True
    # Volume snapshots are a volume capability, so they live with the volume
    # support rather than in the snapshot-specific subclass — a test that only
    # needs volumes should not have to opt into an instance-snapshot fake.
    supports_volume_snapshots = True

    def clone_disk(self, source, target, disk_gb=None):
        self._record("clone", source)
        self.cloned.append((source, target))

    def boot_cloned_instance(self, name, *, cpus, memory, cloud_init_path=None, options=None):
        self._record("boot-clone", name)
        self.state[name] = self._info(name, InstanceStatus.RUNNING, running=True)

    def restart_instance(self, name):
        self.stop_instance(name)
        self.start_instance(name)

    def add_port_forward(self, name, spec):
        self.forwards.setdefault(name, []).append(spec)

    def remove_port_forward(self, name, spec):
        self.forwards[name] = [s for s in self.forwards.get(name, []) if s != spec]

    def create_volume_snapshot(self, volume_path, tag):
        from app.engines.base import SnapshotInfo

        self._volume_snaps.setdefault(str(volume_path), []).append(tag)
        return SnapshotInfo(tag=tag, size_bytes=0)

    def list_volume_snapshots(self, volume_path):
        from app.engines.base import SnapshotInfo

        return [SnapshotInfo(tag=t) for t in self._volume_snaps.get(str(volume_path), [])]

    def restore_volume_snapshot(self, volume_path, tag):
        pass

    def delete_volume_snapshot(self, volume_path, tag):
        tags = self._volume_snaps.get(str(volume_path), [])
        if tag in tags:
            tags.remove(tag)

    def set_volumes(self, name, paths):
        self.volumes[name] = list(paths)

    def get_instance_info(self, name):
        return self._read(self.state.get(name, InstanceInfo(name, False, None, None)))

    def list_instances(self):
        return {name: self._read(info) for name, info in self.state.items()}


@pytest.fixture()
def iso_dir(tmp_path):
    """Isolated boot-media directory, so no test can see the real one."""
    directory = tmp_path / "isos"
    directory.mkdir()
    return directory


# `small_host` lives in conftest.py — every client fixture depends on it, so no
# test's outcome turns on how full the machine running it happens to be.


@pytest.fixture()
def client(monkeypatch, tmp_path, iso_dir, small_host):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    qemu_fake = FakeQemuEngine()
    registry = EngineRegistry({"qemu": lambda: qemu_fake})

    def _session_override():
        with Session(test_engine) as s:
            yield s

    # Background jobs and the startup hooks reach for the module-level DB engine
    # directly, not through DI — every module that does needs redirecting, or
    # its rows land somewhere the request sessions cannot see.
    #
    # Discovered, not listed. The list this replaced was forgotten twice: once
    # for the event writer and once for the projects router, and both times the
    # symptom was rows going quietly missing rather than an error.
    redirect_db_engines(monkeypatch, test_engine)
    monkeypatch.setattr(engines_module, "_registry", registry)

    # Same post-launch settle logic, wound down from 60s/2s so the retry paths
    # are exercised in milliseconds.
    # `state_dir` re-roots every directory the settings own, which is the point:
    # `POST /keypairs/generate` writes a real keypair with real ssh-keygen, and
    # without this it wrote it into the developer's `~/.kurukuru/keys`. Ten
    # test runs left ten orphaned keypairs there before anyone noticed. A test
    # must not be able to touch the state directory of the machine running it.
    test_settings = Settings(
        post_launch_ip_timeout_seconds=1,
        post_launch_poll_seconds=0.001,
        state_dir=str(tmp_path / "state"),
        iso_dir=str(iso_dir),
    )
    monkeypatch.setattr(instances_module, "get_settings", lambda: test_settings)
    # The startup hook that adopts the orchestrator keypair runs outside DI, so
    # the dependency override below does not reach it. Without this it reads —
    # and on a machine that has never run the app, *creates* — the real one.
    monkeypatch.setattr(keypairs_module, "get_settings", lambda: test_settings)

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_engine_registry] = lambda: registry
    app.dependency_overrides[get_settings] = lambda: test_settings

    invalidate_cache()

    with TestClient(app) as c:
        c.fake = qemu_fake  # type: ignore[attr-defined]
        c.qemu_fake = qemu_fake  # type: ignore[attr-defined] - same object
        c.db_engine = test_engine  # type: ignore[attr-defined]
        # Phase 15: the API is closed by default, so a client that does not
        # authenticate can only assert 401s. Done here, once, rather than in
        # several hundred tests — and with a Bearer token rather than a cookie
        # so that existing state-changing calls do not each need a CSRF header
        # too. `anon_client` below is the unauthenticated one.
        authenticate_test_client(c, test_engine)
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def anon_client(client):
    """A *second* client over the same application, carrying no credential.

    Separate rather than the same object with its header stripped: several
    tests need an authenticated and an anonymous caller at once — create a
    token, then prove it works — and mutating one shared client makes those
    quietly test nothing.

    Constructed without the context manager on purpose. `client` already ran
    the lifespan; entering it again would re-run startup against the same
    database.
    """
    c = TestClient(app)
    c.db_engine = client.db_engine  # type: ignore[attr-defined]
    c.fake = client.fake            # type: ignore[attr-defined]
    return c


def legacy_multipass_row(client, name: str, **kwargs) -> str:
    """Insert a row from the retired Multipass engine, as a migrated DB has."""
    defaults = {
        "engine": "multipass",
        "status": InstanceStatus.RUNNING,
        "ip_address": "10.1.2.3",
    }
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = Instance(name=name, **{**defaults, **kwargs})
        session.add(row)
        session.commit()
        return row.id


def test_create_returns_202_and_provisions_to_running(client):
    r = client.post("/instances", json={"name": "web-one", "flavor": "small"})
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "Pending"  # response reflects the accepted record

    # Background provisioning ran synchronously -> now Running with an IP.
    got = client.get(f"/instances/{body['id']}").json()
    assert got["status"] == "Running"
    assert got["ip_address"] == FAKE_IP


def test_duplicate_name_is_409(client):
    client.post("/instances", json={"name": "dupe", "flavor": "small"})
    r = client.post("/instances", json={"name": "dupe", "flavor": "small"})
    assert r.status_code == 409


def test_get_missing_is_404(client):
    assert client.get("/instances/does-not-exist").status_code == 404


def test_stop_then_start_state_machine(client):
    inst_id = client.post("/instances", json={"name": "sm", "flavor": "small"}).json()["id"]

    # Running -> stop OK
    assert client.post(f"/instances/{inst_id}/stop").status_code == 200
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Stopped"

    # Stopped -> stop again = 409
    assert client.post(f"/instances/{inst_id}/stop").status_code == 409

    # Stopped -> start OK
    assert client.post(f"/instances/{inst_id}/start").status_code == 200
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Running"

    # Running -> start = 409
    assert client.post(f"/instances/{inst_id}/start").status_code == 409


def test_delete_marks_terminated_and_hides_from_list(client):
    inst_id = client.post("/instances", json={"name": "goner", "flavor": "small"}).json()["id"]
    assert client.delete(f"/instances/{inst_id}").status_code == 200
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Terminated"

    # default list excludes terminated; include_terminated shows it
    names = [i["name"] for i in client.get("/instances").json()]
    assert "goner" not in names
    all_names = [i["name"] for i in client.get("/instances?include_terminated=true").json()]
    assert "goner" in all_names


# --------------------------------------------------------------------------- #
# Name reuse: unique among live instances, free again once Terminated
# --------------------------------------------------------------------------- #
def test_name_is_reusable_after_termination(client):
    first = client.post("/instances", json={"name": "recycle", "flavor": "small"}).json()
    assert client.delete(f"/instances/{first['id']}").status_code == 200

    # The audit row still holds the name, but it no longer reserves it.
    second = client.post("/instances", json={"name": "recycle", "flavor": "small"})
    assert second.status_code == 202
    assert second.json()["id"] != first["id"]
    assert client.get(f"/instances/{second.json()['id']}").json()["status"] == "Running"


def test_name_reuse_starts_a_clean_row(client):
    """A freed name carries nothing over from the instance that held it."""
    first = client.post(
        "/instances", json={"name": "swap", "flavor": "small", "accel": "tcg"}
    ).json()
    client.delete(f"/instances/{first['id']}")

    second = client.post("/instances", json={"name": "swap", "flavor": "small"}).json()
    assert second["id"] != first["id"]
    assert second["engine"] == "qemu"
    assert second["accel"] is None  # not inherited from the previous row


def test_duplicate_name_still_409s_while_a_live_row_exists(client):
    client.post("/instances", json={"name": "live", "flavor": "small"})
    r = client.post("/instances", json={"name": "live", "flavor": "small"})
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


@pytest.mark.parametrize("state", ["Running", "Stopped", "Error"])
def test_every_non_terminated_state_reserves_the_name(client, state):
    """Only Terminated frees a name — a Stopped or Error row still owns it."""
    inst_id = client.post("/instances", json={"name": "held", "flavor": "small"}).json()["id"]

    if state == "Stopped":
        client.post(f"/instances/{inst_id}/stop")
    elif state == "Error":
        client.fake.state.pop("held")  # type: ignore[attr-defined]
        client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["status"] == state

    assert client.post("/instances", json={"name": "held", "flavor": "small"}).status_code == 409


def test_audit_rows_with_duplicate_names_coexist(client):
    """Several terminated generations of one name are all retained."""
    ids = []
    for _ in range(3):
        created = client.post("/instances", json={"name": "gen", "flavor": "small"}).json()
        ids.append(created["id"])
        client.delete(f"/instances/{created['id']}")

    live = client.post("/instances", json={"name": "gen", "flavor": "small"}).json()

    everything = client.get("/instances?include_terminated=true").json()
    gens = [i for i in everything if i["name"] == "gen"]
    assert len(gens) == 4  # three audit rows + the live one
    assert len({i["id"] for i in gens}) == 4
    assert sorted(i["status"] for i in gens) == ["Running", "Terminated", "Terminated", "Terminated"]

    # The default listing shows only the live generation.
    assert [i["id"] for i in client.get("/instances").json() if i["name"] == "gen"] == [live["id"]]


def test_reconciler_only_matches_the_live_row_for_a_reused_name(client):
    """A stale audit row must not be revived by the new VM's listing entry."""
    old = client.post("/instances", json={"name": "reused", "flavor": "small"}).json()
    client.delete(f"/instances/{old['id']}")
    new = client.post("/instances", json={"name": "reused", "flavor": "small"}).json()

    client.post("/instances/refresh")

    # The hypervisor reports one VM named "reused"; it belongs to the new row.
    assert client.get(f"/instances/{new['id']}").json()["status"] == "Running"
    assert client.get(f"/instances/{old['id']}").json()["status"] == "Terminated"


def test_reconciler_leaves_audit_rows_alone_when_the_name_is_gone(client):
    """A terminated row must not be flipped to Error just because no VM exists."""
    old = client.post("/instances", json={"name": "ghosted", "flavor": "small"}).json()
    client.delete(f"/instances/{old['id']}")

    client.post("/instances/refresh")
    assert client.get(f"/instances/{old['id']}").json()["status"] == "Terminated"


def test_deleting_a_stale_audit_row_does_not_touch_the_live_vm(client):
    """Terminate is a no-op on an already-terminated row — otherwise it would
    destroy by name and take down the instance that reused it."""
    old = client.post("/instances", json={"name": "shared", "flavor": "small"}).json()
    client.delete(f"/instances/{old['id']}")
    new = client.post("/instances", json={"name": "shared", "flavor": "small"}).json()
    client.fake.calls.clear()  # type: ignore[attr-defined]

    assert client.delete(f"/instances/{old['id']}").status_code == 200

    assert client.fake.calls == []  # type: ignore[attr-defined] - no destroy issued
    assert "shared" in client.fake.state  # type: ignore[attr-defined]
    assert client.get(f"/instances/{new['id']}").json()["status"] == "Running"


def test_start_from_terminated_is_409(client):
    inst_id = client.post("/instances", json={"name": "term", "flavor": "small"}).json()["id"]
    client.delete(f"/instances/{inst_id}")
    assert client.post(f"/instances/{inst_id}/start").status_code == 409


def test_refresh_reconciles_vanished_vm_to_error(client):
    inst_id = client.post("/instances", json={"name": "vanish", "flavor": "small"}).json()["id"]
    # Simulate the VM disappearing from the hypervisor out-of-band.
    client.fake.state.pop("vanish")  # type: ignore[attr-defined]

    client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Error"


# --------------------------------------------------------------------------- #
# Phase 5: engine selection and per-engine dispatch
# --------------------------------------------------------------------------- #
def test_engine_defaults_to_qemu(client):
    body = client.post("/instances", json={"name": "implicit", "flavor": "small"}).json()
    assert body["engine"] == "qemu"
    assert ("provision", "implicit") in client.fake.calls  # type: ignore[attr-defined]


def test_unknown_engine_is_422(client):
    r = client.post("/instances", json={"name": "bad", "flavor": "small", "engine": "xen"})
    assert r.status_code == 422


def test_instance_exposes_the_engine_runtime_ports(client):
    body = client.post("/instances", json={"name": "qemu-one", "flavor": "small"}).json()
    assert body["engine"] == "qemu"

    got = client.get(f"/instances/{body['id']}").json()
    assert got["status"] == "Running"
    assert got["ip_address"] == FAKE_IP
    # Runtime detail is mirrored onto the row for the dashboard's Copy SSH.
    assert (got["ssh_port"], got["vnc_port"], got["qmp_port"]) == (2222, 5900, 4444)
    assert got["pid"] == 4242


def test_stop_start_reaches_the_rows_engine(client):
    qemu_id = client.post("/instances", json={"name": "q-vm", "flavor": "small"}).json()["id"]

    client.post(f"/instances/{qemu_id}/stop")
    stopped = client.get(f"/instances/{qemu_id}").json()
    assert stopped["status"] == "Stopped"
    # Ports stay pinned while stopped; only the pid clears.
    assert stopped["ssh_port"] == 2222
    assert stopped["pid"] is None

    client.post(f"/instances/{qemu_id}/start")
    assert client.get(f"/instances/{qemu_id}").json()["status"] == "Running"

    assert [c[0] for c in client.fake.calls] == [  # type: ignore[attr-defined]
        "provision", "stop", "start",
    ]


def test_reconciler_skips_rows_from_a_retired_engine(client):
    """A legacy Multipass row has no driver to consult, so nothing may change it.

    Treating "no driver" like "VM missing" would flip every historical row to
    Error on the first pass after the retirement — rewriting the record to claim
    those instances failed, when their hypervisor was removed underneath them.
    """
    legacy = legacy_multipass_row(client, "old-mp")
    live = client.post("/instances", json={"name": "new-qemu", "flavor": "small"}).json()["id"]

    client.post("/instances/refresh")

    row = client.get(f"/instances/{legacy}").json()
    assert row["status"] == "Running"          # untouched
    assert row["ip_address"] == "10.1.2.3"     # last known state preserved
    assert row["error_message"] is None
    # ...and the live QEMU row reconciled normally in the same pass.
    assert client.get(f"/instances/{live}").json()["status"] == "Running"


def test_retired_engine_rows_survive_repeated_passes(client):
    """Not a one-pass reprieve — they must stay stable indefinitely."""
    legacy = legacy_multipass_row(client, "persistent-mp", status=InstanceStatus.STOPPED)

    for _ in range(3):
        client.post("/instances/refresh")

    row = client.get(f"/instances/{legacy}").json()
    assert row["status"] == "Stopped"
    assert row["error_message"] is None


def test_retired_engine_row_still_appears_in_the_listing(client):
    """Audit rows must remain visible, engine label and all."""
    legacy_multipass_row(client, "visible-mp")

    listed = {i["name"]: i for i in client.get("/instances").json()}
    assert "visible-mp" in listed
    assert listed["visible-mp"]["engine"] == "multipass"
    # No driver means no console, whatever the row says.
    assert listed["visible-mp"]["console_supported"] is False


def test_retired_engine_row_is_force_terminable(client):
    """The one action left: clear the record, with no driver involved."""
    legacy = legacy_multipass_row(client, "goodbye-mp")

    r = client.delete(f"/instances/{legacy}")
    assert r.status_code == 200
    assert r.json()["status"] == "Terminated"
    # Nothing was asked of the QEMU driver on another engine's behalf.
    assert client.fake.calls == []  # type: ignore[attr-defined]

    assert "goodbye-mp" not in [i["name"] for i in client.get("/instances").json()]


@pytest.mark.parametrize("action", ["start", "stop"])
def test_retired_engine_row_cannot_be_started_or_stopped(client, action):
    """There is no hypervisor left to drive — say so instead of 500ing."""
    legacy = legacy_multipass_row(
        client,
        f"frozen-{action}",
        status=InstanceStatus.STOPPED if action == "start" else InstanceStatus.RUNNING,
    )

    r = client.post(f"/instances/{legacy}/{action}")
    assert r.status_code == 409
    assert "retired" in r.json()["detail"]
    assert "multipass" in r.json()["detail"]


def test_a_retired_row_does_not_block_reusing_its_name(client):
    """Once terminated, a legacy name is free like any other."""
    legacy = legacy_multipass_row(client, "recycled")
    assert client.post("/instances", json={"name": "recycled", "flavor": "small"}).status_code == 409

    client.delete(f"/instances/{legacy}")
    fresh = client.post("/instances", json={"name": "recycled", "flavor": "small"})
    assert fresh.status_code == 202
    assert fresh.json()["engine"] == "qemu"


def test_terminate_clears_runtime_ports(client):
    qemu_id = client.post(
        "/instances", json={"name": "q-gone", "flavor": "small", "engine": "qemu"}
    ).json()["id"]
    client.delete(f"/instances/{qemu_id}")

    row = client.get(f"/instances/{qemu_id}").json()
    assert row["status"] == "Terminated"
    assert row["ssh_port"] is None and row["pid"] is None
    assert ("destroy", "q-gone") in client.qemu_fake.calls  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# ip_address population — a Running row must never be left without an address
# --------------------------------------------------------------------------- #
def test_reconciler_populates_ip_for_qemu_rows(client):
    """Direct guard against the per-engine dispatch dropping IPs on the floor."""
    inst_id = client.post("/instances", json={"name": "q-ip", "flavor": "small"}).json()["id"]

    # Blank the row as if it had been left Running-with-no-IP, then reconcile.
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = session.get(Instance, inst_id)
        row.ip_address = None
        session.add(row)
        session.commit()
    assert client.get(f"/instances/{inst_id}").json()["ip_address"] is None

    client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["ip_address"] == FAKE_IP


def test_provisioning_waits_for_a_late_ip(client):
    """The hypervisor reports Running before publishing the address.

    Sampling once would write Running-with-no-IP and — since nothing reconciles
    again on its own — leave the dashboard showing "—" indefinitely.
    """
    client.fake.withhold_ip_reads = 3  # type: ignore[attr-defined]

    body = client.post("/instances", json={"name": "late-ip", "flavor": "small"}).json()
    row = client.get(f"/instances/{body['id']}").json()

    assert row["status"] == "Running"
    assert row["ip_address"] == FAKE_IP
    assert client.fake.read_count > 1  # type: ignore[attr-defined] - it retried


def test_provisioning_recovers_from_a_transient_read_failure(client):
    """A hypervisor that is briefly unreachable right after launch."""
    client.fake.fail_reads = 2  # type: ignore[attr-defined]

    body = client.post("/instances", json={"name": "flaky", "flavor": "small"}).json()
    row = client.get(f"/instances/{body['id']}").json()

    assert row["status"] == "Running"
    assert row["ip_address"] == FAKE_IP


def test_provisioning_still_reports_running_when_reads_never_succeed(client):
    """A wedged hypervisor must not strand the row in Provisioning...

    ...and the next reconcile must then fill in what was missed, rather than the
    row staying wrong until a human presses Refresh.
    """
    client.fake.fail_reads = 10_000  # type: ignore[attr-defined]

    body = client.post("/instances", json={"name": "wedged", "flavor": "small"}).json()
    row = client.get(f"/instances/{body['id']}").json()
    assert row["status"] == "Running"
    assert row["ip_address"] is None

    client.fake.fail_reads = 0  # type: ignore[attr-defined] - hypervisor recovers
    client.post("/instances/refresh")
    assert client.get(f"/instances/{body['id']}").json()["ip_address"] == FAKE_IP


def test_reconcile_does_not_announce_running_mid_launch(client):
    """A reconcile landing mid-provision must not overtake the launch job.

    Reproduced live: the background pass caught a Multipass VM in "Starting",
    wrote Running with no address, and the dashboard showed "Running, —" with a
    Copy SSH button for a guest that wasn't listening yet.
    """
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = Instance(name="mid-launch", status=InstanceStatus.PROVISIONING)
        session.add(row)
        session.commit()
        row_id = row.id

    # The hypervisor knows the VM but it is still coming up: no address yet.
    client.fake.state["mid-launch"] = InstanceInfo(  # type: ignore[attr-defined]
        "mid-launch", True, InstanceStatus.PROVISIONING, None
    )
    client.post("/instances/refresh")
    assert client.get(f"/instances/{row_id}").json()["status"] == "Provisioning"

    # Even a Running report is ignored until an address comes with it — that is
    # what distinguishes "booted" from "usable".
    client.fake.state["mid-launch"] = InstanceInfo(  # type: ignore[attr-defined]
        "mid-launch", True, InstanceStatus.RUNNING, None
    )
    client.post("/instances/refresh")
    row = client.get(f"/instances/{row_id}").json()
    assert row["status"] == "Provisioning"
    assert row["ip_address"] is None

    # Ready for real: advance.
    client.fake.state["mid-launch"] = InstanceInfo(  # type: ignore[attr-defined]
        "mid-launch", True, InstanceStatus.RUNNING, FAKE_IP
    )
    client.post("/instances/refresh")
    row = client.get(f"/instances/{row_id}").json()
    assert row["status"] == "Running"
    assert row["ip_address"] == FAKE_IP


def test_a_running_row_is_never_left_without_an_ip(client):
    """The invariant behind the bug: Running implies an address."""
    client.post("/instances", json={"name": "inv-mp", "flavor": "small"})
    client.post("/instances", json={"name": "inv-q", "flavor": "small", "engine": "qemu"})
    client.post("/instances/refresh")

    for row in client.get("/instances").json():
        if row["status"] == "Running":
            assert row["ip_address"], f"{row['name']} is Running with no IP"


def test_terminating_during_a_reconcile_pass_stays_terminated(client):
    """Regression: a terminate landing mid-pass must not be undone.

    The reconciler gathers listings first and applies them after. If a DELETE
    commits in that window, applying a stale snapshot resurrects the row — and
    the next pass, seeing no VM, marks the just-terminated instance Error.
    """
    inst_id = client.post("/instances", json={"name": "racy", "flavor": "small"}).json()["id"]

    # Stand in for the window: the row is terminated after the pass's rows were
    # loaded but before its snapshot is applied.
    original = instances_module._apply_info
    state = {"done": False}

    def terminate_midway(instance, info, **kwargs):
        if not state["done"] and instance.name == "racy":
            state["done"] = True
            client.delete(f"/instances/{inst_id}")
        return original(instance, info, **kwargs)

    instances_module._apply_info = terminate_midway
    try:
        client.post("/instances/refresh")
    finally:
        instances_module._apply_info = original

    row = client.get(f"/instances/{inst_id}").json()
    assert row["status"] == "Terminated"

    # And a later pass must not resurrect it as Error either.
    client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Terminated"


def test_delete_during_the_listing_phase_is_not_overwritten(client):
    """The transaction-boundary guarantee, tested where the window actually is.

    Listing a hypervisor takes seconds. A terminate landing in that window used
    to be overwritten by the pass's stale snapshot, and the *next* pass — now
    seeing no VM — marked the just-terminated instance Error. Rows are read
    after the I/O now, so the delete is already visible when we decide.
    """
    inst_id = client.post("/instances", json={"name": "raced", "flavor": "small"}).json()["id"]

    original_list = client.fake.list_instances  # type: ignore[attr-defined]
    fired = {"done": False}

    def delete_while_listing():
        # Stand in for a terminate arriving while the hypervisor is being asked.
        if not fired["done"]:
            fired["done"] = True
            client.delete(f"/instances/{inst_id}")
        return original_list()

    client.fake.list_instances = delete_while_listing  # type: ignore[attr-defined]
    try:
        client.post("/instances/refresh")
    finally:
        client.fake.list_instances = original_list  # type: ignore[attr-defined]

    assert client.get(f"/instances/{inst_id}").json()["status"] == "Terminated"

    # And a later pass must not resurrect it as Error either.
    client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Terminated"


def test_reconcile_pass_survives_the_engine_being_down(client):
    """An unreachable hypervisor must not rewrite rows it cannot see."""
    inst_id = client.post("/instances", json={"name": "q-up", "flavor": "small"}).json()["id"]

    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = session.get(Instance, inst_id)
        row.ip_address = None
        session.add(row)
        session.commit()

    client.fake.fail_reads = 10_000  # type: ignore[attr-defined]
    assert client.post("/instances/refresh").status_code == 200
    assert client.get(f"/instances/{inst_id}").json()["ip_address"] is None

    client.fake.fail_reads = 0  # type: ignore[attr-defined] - hypervisor recovers
    client.post("/instances/refresh")
    assert client.get(f"/instances/{inst_id}").json()["ip_address"] == FAKE_IP


def test_unreachable_engine_leaves_its_rows_untouched(client):
    """Never mark a row Error just because its hypervisor didn't answer."""
    inst_id = client.post("/instances", json={"name": "unseen", "flavor": "small"}).json()["id"]

    client.fake.fail_reads = 10_000  # type: ignore[attr-defined]
    client.post("/instances/refresh")

    row = client.get(f"/instances/{inst_id}").json()
    assert row["status"] == "Running"
    assert row["ip_address"] == FAKE_IP
    assert row["error_message"] is None


# --------------------------------------------------------------------------- #
# ISO boot (Phase 6, Part B)
# --------------------------------------------------------------------------- #
def test_iso_instance_records_boot_source_and_media(client, iso_dir):
    (iso_dir / "alpine.iso").write_bytes(b"\0" * 64)

    body = client.post(
        "/instances",
        json={"name": "from-iso", "flavor": "small", "engine": "qemu", "iso": "alpine.iso"},
    ).json()

    assert body["boot_source"] == "iso"
    assert body["iso"] == "alpine.iso"


def test_retired_engine_is_refused_with_an_explanation(client):
    """Naming Multipass must say it was retired, not just "unknown"."""
    r = client.post(
        "/instances", json={"name": "old-way", "flavor": "small", "engine": "multipass"}
    )
    assert r.status_code == 422
    detail = str(r.json()["detail"])
    assert "no longer supported" in detail
    assert "qemu" in detail


def test_missing_iso_is_rejected_at_request_time(client, iso_dir):
    """Better a 422 now than a 202 followed by a mysterious Error row."""
    r = client.post(
        "/instances",
        json={"name": "ghost-iso", "flavor": "small", "engine": "qemu", "iso": "nope.iso"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("attack", ["../escape.iso", "/etc/shadow", "sub/deep.iso"])
def test_iso_path_traversal_is_refused(client, iso_dir, attack):
    r = client.post(
        "/instances",
        json={"name": "evil", "flavor": "small", "engine": "qemu", "iso": attack},
    )
    assert r.status_code == 422


def test_isos_endpoint_lists_available_media(client, iso_dir):
    (iso_dir / "alpine.iso").write_bytes(b"\0" * 128)
    (iso_dir / "readme.txt").write_text("ignored")

    listing = client.get("/isos").json()
    assert [i["name"] for i in listing] == ["alpine.iso"]
    assert listing[0]["size_bytes"] == 128


def test_iso_instance_reaches_running_without_an_ip(client, iso_dir):
    """Regression: the mid-launch guard must not strand address-less guests.

    The Part A guard treated "has an IP" as the definition of ready, which is
    true for cloud images and false for every ISO guest — those got stuck in
    Provisioning forever because the condition could never be met.
    """
    (iso_dir / "alpine.iso").write_bytes(b"\0" * 64)
    # An ISO guest reports Running with no address, exactly like the real engine.
    client.qemu_fake.iso_mode = True  # type: ignore[attr-defined]

    body = client.post(
        "/instances",
        json={"name": "iso-ready", "flavor": "small", "engine": "qemu", "iso": "alpine.iso"},
    ).json()

    row = client.get(f"/instances/{body['id']}").json()
    assert row["status"] == "Running"
    assert row["ip_address"] is None
    assert row["ssh_port"] is None


def test_background_reconcile_still_cannot_promote_a_provisioning_row(client):
    """The guard must survive the fix above: only the launch job is authoritative."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = Instance(name="mid", engine="qemu", status=InstanceStatus.PROVISIONING)
        session.add(row)
        session.commit()
        row_id = row.id

    client.qemu_fake.state["mid"] = InstanceInfo(  # type: ignore[attr-defined]
        "mid", True, InstanceStatus.RUNNING, None
    )
    client.post("/instances/refresh")
    assert client.get(f"/instances/{row_id}").json()["status"] == "Provisioning"


def test_console_is_not_promised_before_the_accelerator_is_known(client):
    """'auto' is a request, not a fact — don't offer a console we can't deliver."""
    body = client.post(
        "/instances", json={"name": "pending-accel", "flavor": "small", "engine": "qemu"}
    ).json()
    assert body["accel"] is None
    assert body["console_supported"] is False


# --------------------------------------------------------------------------- #
# Custom resources (Phase 7, Part A)
# --------------------------------------------------------------------------- #
def test_a_preset_fills_in_the_numbers(client):
    body = client.post("/instances", json={"name": "preset-sized", "preset": "medium"}).json()
    assert (body["cpus"], body["memory_mb"], body["disk_gb"]) == (2, 2048, 10)
    assert body["flavor"] == "medium"  # label records where it started


def test_explicit_numbers_are_used_verbatim(client):
    body = client.post(
        "/instances",
        json={"name": "hand-sized", "cpus": 3, "memory_mb": 3072, "disk_gb": 12},
    ).json()
    assert (body["cpus"], body["memory_mb"], body["disk_gb"]) == (3, 3072, 12)
    assert body["flavor"] == "custom"


def test_explicit_fields_override_a_preset(client, small_host):
    """"Large, but with more disk" must be expressible without a new preset."""
    body = client.post(
        "/instances", json={"name": "large-plus", "preset": "large", "disk_gb": 40}
    ).json()
    assert (body["cpus"], body["memory_mb"]) == (4, 4096)  # from the preset
    assert body["disk_gb"] == 40                            # overridden
    assert body["flavor"] == "custom"


def test_flavor_is_still_accepted_as_a_preset_alias(client):
    """Existing clients post `flavor`; they must keep working."""
    body = client.post("/instances", json={"name": "legacy-call", "flavor": "large"}).json()
    assert (body["cpus"], body["memory_mb"], body["disk_gb"]) == (4, 4096, 20)


def test_unknown_preset_is_422(client):
    r = client.post("/instances", json={"name": "bad-preset", "preset": "enormous"})
    assert r.status_code == 422
    assert "enormous" in str(r.json()["detail"])


@pytest.mark.parametrize(
    "field,value,phrase",
    [
        ("cpus", 0, "positive"),
        ("memory_mb", -1, "positive"),
        ("disk_gb", 0, "positive"),
    ],
)
def test_nonsense_sizes_are_rejected(client, field, value, phrase):
    r = client.post("/instances", json={"name": "nonsense", field: value})
    assert r.status_code == 422
    assert phrase in str(r.json()["detail"]).lower()


def test_below_minimum_memory_is_rejected_with_the_floor(client):
    r = client.post("/instances", json={"name": "tiny", "memory_mb": 128})
    assert r.status_code == 422
    assert "at least 512 MB" in str(r.json()["detail"])


def test_over_capacity_memory_is_rejected_with_the_arithmetic(client, small_host):
    """The refusal must name the limit and how it was arrived at.

    "Too big" is not actionable; the reserve and the committed total are what
    let a user decide whether to stop something or ask for less.
    """
    r = client.post("/instances", json={"name": "greedy", "memory_mb": 15000})
    assert r.status_code == 422

    detail = r.json()["detail"]
    assert "15000 MB" in detail          # what was asked for
    assert "allocatable" in detail       # what is available
    assert "host reserve" in detail      # why it is less than the total
    assert "committed" in detail


def test_over_capacity_cpus_is_rejected(client, small_host):
    r = client.post("/instances", json={"name": "many-cpus", "cpus": 64})
    assert r.status_code == 422
    assert "vCPUs" in r.json()["detail"]


def test_over_capacity_disk_is_rejected(client, small_host):
    r = client.post("/instances", json={"name": "fat-disk", "disk_gb": 9999})
    assert r.status_code == 422
    assert "free" in r.json()["detail"]


def test_running_instances_reduce_what_the_next_launch_may_have(client, small_host):
    """The check is against *remaining* capacity, not the empty-host figure."""
    # 16384 total - 2048 reserve = 14336 allocatable, so one 8192 fits...
    assert client.post(
        "/instances", json={"name": "first", "memory_mb": 8192}
    ).status_code == 202

    # ...and the second sees only 6144 left.
    r = client.post("/instances", json={"name": "second", "memory_mb": 8192})
    assert r.status_code == 422
    assert "8192 MB" in r.json()["detail"]
    assert "8192 MB committed" in r.json()["detail"]


def test_capacity_endpoint_reports_totals_committed_and_allocatable(client, small_host):
    client.post("/instances", json={"name": "occupier", "cpus": 2, "memory_mb": 2048})

    cap = client.get("/host/capacity").json()
    assert cap["memory_mb"]["total"] == 16384
    assert cap["memory_mb"]["committed"] == 2048
    assert cap["memory_mb"]["allocatable"] == 16384 - 2048 - 2048
    assert cap["cpu"]["committed"] == 2
    assert cap["degraded"] is False


def test_a_degraded_capacity_probe_does_not_block_launches(client):
    """Refusing every launch because the *capacity check* broke would be a
    worse failure than the one it prevents."""
    with patch("app.host_capacity.psutil", None):
        invalidate_cache()
        r = client.post("/instances", json={"name": "permissive", "memory_mb": 4096})
    invalidate_cache()
    assert r.status_code == 202


def test_disk_must_not_be_smaller_than_the_backing_image(client, small_host):
    """A qcow2 overlay shorter than its backing file would present the guest a
    disk smaller than the filesystem already on it."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        image = Image(
            name="Twenty GB image",
            filename="twenty.qcow2",
            source=ImageSource.IMPORTED,
            status=ImageStatus.AVAILABLE,
            has_cloud_init=True,
            virtual_size_bytes=20 * 1024**3,
        )
        session.add(image)
        session.commit()
        image_id = image.id

    r = client.post(
        "/instances", json={"name": "too-small", "disk_gb": 5, "image_id": image_id}
    )
    assert r.status_code == 422
    assert "at least 20 GB" in r.json()["detail"]
    assert "shrinking" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Degraded indicator (Phase 7, Part C)
# --------------------------------------------------------------------------- #
def _aged_row(client, name, *, minutes, **kwargs):
    """A row whose last update was `minutes` ago, to age past the threshold."""
    from datetime import timedelta

    from app.models import _utcnow

    defaults = {
        "engine": "qemu",
        "status": InstanceStatus.RUNNING,
        "ssh_enabled": True,
        "ip_address": None,
        "accel": "whpx",
    }
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = Instance(name=name, **{**defaults, **kwargs})
        row.updated_at = _utcnow().replace(tzinfo=None) - timedelta(minutes=minutes)
        session.add(row)
        session.commit()
        return row.id


def test_a_running_instance_without_its_address_becomes_degraded(client):
    """A green "Running" on something you cannot SSH into is the UI lying."""
    row = _aged_row(client, "stuck", minutes=5)
    body = client.get(f"/instances/{row}").json()

    assert body["degraded"] is True
    assert "no address" in body["degraded_reason"]


def test_a_recently_started_instance_is_not_yet_degraded(client):
    """Boot takes time; flagging it immediately would cry wolf on every launch."""
    row = _aged_row(client, "just-booted", minutes=0)
    assert client.get(f"/instances/{row}").json()["degraded"] is False


def test_console_only_instances_are_never_degraded(client):
    """An ISO guest has no address by design — that is working, not broken."""
    row = _aged_row(client, "console-only", minutes=10, ssh_enabled=False)
    body = client.get(f"/instances/{row}").json()

    assert body["degraded"] is False
    assert body["degraded_reason"] is None


def test_an_instance_with_its_address_is_not_degraded(client):
    row = _aged_row(client, "healthy", minutes=10, ip_address=FAKE_IP)
    assert client.get(f"/instances/{row}").json()["degraded"] is False


@pytest.mark.parametrize("state", [InstanceStatus.STOPPED, InstanceStatus.ERROR])
def test_only_running_instances_can_be_degraded(client, state):
    row = _aged_row(client, f"not-running-{state.value.lower()}", minutes=10, status=state)
    assert client.get(f"/instances/{row}").json()["degraded"] is False


# --------------------------------------------------------------------------- #
# Health reports the engine (Phase 7, Part C)
# --------------------------------------------------------------------------- #
def test_health_reports_engine_status(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["engine"]["available"] is True
    assert body["engine"]["name"] == "qemu"


def test_health_is_degraded_when_the_engine_is_unavailable(client):
    """A control plane answering "ok" while its hypervisor is broken sends
    people debugging the wrong layer."""
    client.fake.available = False  # type: ignore[attr-defined]

    body = client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["engine"]["available"] is False


def test_iso_launch_defaults_to_hardware_acceleration(client, iso_dir):
    """ISO instances no longer pay a boot-speed penalty for the console.

    The forced-TCG rule was based on "accelerated VMs can't render a console",
    which holds only for VGA-text-mode guests — not for ISO installers, which
    switch to a framebuffer.
    """
    (iso_dir / "alpine.iso").write_bytes(b"\0" * 64)
    client.qemu_fake.iso_mode = True  # type: ignore[attr-defined]

    body = client.post(
        "/instances",
        json={"name": "fast-iso", "flavor": "small", "iso": "alpine.iso"},
    ).json()

    # 'auto' is stored as None; the engine resolves it, and nothing in the
    # request path may pin it to tcg on the instance's behalf.
    assert body["accel"] is None
    assert body["boot_source"] == "iso"
    assert client.get(f"/instances/{body['id']}").json()["accel"] == "whpx"


def test_display_defaults_to_standard_graphics(client):
    body = client.post("/instances", json={"name": "plain-gfx", "flavor": "small"}).json()
    assert body["display"] == "std"


def test_display_can_be_requested_as_virtio(client):
    body = client.post(
        "/instances", json={"name": "modern-gfx", "flavor": "small", "display": "virtio"}
    ).json()
    assert body["display"] == "virtio"


def test_unknown_display_is_422(client):
    r = client.post(
        "/instances", json={"name": "bad-gfx", "flavor": "small", "display": "cirrus"}
    )
    assert r.status_code == 422
    assert "cirrus" in str(r.json()["detail"])


def test_default_boot_source_is_image(client):
    body = client.post("/instances", json={"name": "plain", "flavor": "small"}).json()
    assert body["boot_source"] == "image"
    assert body["iso"] is None


def test_engines_endpoint_lists_only_live_drivers(client):
    catalog = client.get("/engines").json()
    assert {e["name"] for e in catalog} == {"qemu"}
    assert all(e["available"] for e in catalog)


# --------------------------------------------------------------------------- #
# The first launch on a fresh install (found on the first Linux host)
# --------------------------------------------------------------------------- #
def _builtin_row(client, status: InstanceStatus | ImageStatus) -> str:
    """Put the built-in image row into a given state and return its id."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        image = session.exec(
            select(Image).where(Image.source == ImageSource.BUILTIN)
        ).first()
        if image is None:
            image = Image(
                name="Ubuntu 24.04 LTS (cloud)",
                filename="noble-server-cloudimg-amd64.img",
                source=ImageSource.BUILTIN,
                has_cloud_init=True,
            )
        image.status = status
        session.add(image)
        session.commit()
        return image.id


def test_first_launch_works_before_the_base_image_is_downloaded(client):
    """The fresh-install path: nothing on disk yet, and it must still launch.

    The built-in image is fetched on demand by the engine, so its row sits at
    `Importing` until something asks for it. Requiring `Available` refused the
    first launch on every brand-new install — for the absence of a file the
    launch itself was there to download — and then worked on the second
    attempt, because the failed first one had left the file behind.

    Invisible on any machine that has ever launched an instance, which is every
    developer machine. Found by running on a host that never had.
    """
    _builtin_row(client, ImageStatus.IMPORTING)

    body = client.post("/instances", json={"name": "fresh", "flavor": "small"})
    assert body.status_code == 202

    row = client.get(f"/instances/{body.json()['id']}").json()
    assert row["status"] == "Running", row["error_message"]


def test_a_broken_builtin_image_still_refuses(client):
    """`Error` means the file is there and unreadable — downloading won't help."""
    _builtin_row(client, ImageStatus.ERROR)

    body = client.post("/instances", json={"name": "broken-base", "flavor": "small"})
    assert body.status_code == 202  # accepted, then fails in the background job

    row = client.get(f"/instances/{body.json()['id']}").json()
    assert row["status"] == "Error"
    assert "not Available" in (row["error_message"] or "")


def test_an_imported_image_still_has_to_be_available(client):
    """Only the built-in image is fetched on demand; nothing downloads the rest."""
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        image = Image(
            name="half-copied",
            filename="imported-abc.qcow2",
            source=ImageSource.IMPORTED,
            status=ImageStatus.IMPORTING,
            has_cloud_init=True,
        )
        session.add(image)
        session.commit()
        image_id = image.id

    r = client.post(
        "/instances", json={"name": "too-soon", "flavor": "small", "image_id": image_id}
    )
    assert r.status_code == 409
    assert "not Available" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Diagnostics and force-terminate (Phase 8 — what the CLI needs from the API)
# --------------------------------------------------------------------------- #
def test_diagnostics_reports_the_host_facts_a_client_cannot_see(client):
    """`kurukuru doctor` is only honest if these come from the backend's own host.

    A CLI that probed its own filesystem for the instance store would describe
    the wrong machine the moment the two are not the same.
    """
    body = client.get("/diagnostics").json()

    assert body["api"]["version"]
    assert body["python"]
    assert body["engine"]["available"] is True
    # Facts, not verdicts: no pass/fail and no remedies, so every client can
    # apply its own idea of healthy.
    assert set(body["instance_store"]) >= {"path", "exists", "writable", "free_bytes"}
    assert body["ssh_key"]["present"] is True


def test_diagnostics_exposes_no_key_material(client):
    """Nothing here is authenticated, so the payload stays to the minimum.

    `kurukuru doctor` needs to know a keypair *exists* and where it lives; it never
    needs the key itself. Callers who want the public half ask /ssh-key, which
    is the endpoint that exists for it. Guarded because the natural way to
    write this endpoint is to paste the /ssh-key body in.
    """
    body = client.get("/diagnostics").json()

    assert "public_key" not in body["ssh_key"]
    assert "ssh-ed25519" not in json.dumps(body)
    assert "PRIVATE KEY" not in json.dumps(body)


def test_diagnostics_survives_a_broken_engine(client):
    """It is most needed exactly when something is wrong."""
    client.fake.available = False  # type: ignore[attr-defined]

    body = client.get("/diagnostics").json()
    assert body["engine"]["available"] is False
    assert body["instance_store"]["path"]  # the rest still answers


def test_force_terminate_clears_a_row_the_hypervisor_will_not_destroy(client):
    """A wedged hypervisor must not leave a row nothing can clear.

    Without force this is a 502 and the row is marked Error, which is right:
    saying Terminated while the VM is still running would be a lie. With force
    the caller has accepted that trade.
    """
    inst_id = client.post("/instances", json={"name": "wedged", "flavor": "small"}).json()["id"]

    def _refuse(name: str) -> None:
        raise ComputeEngineError("qemu: cannot destroy, process is unkillable")

    client.fake.destroy_instance = _refuse  # type: ignore[attr-defined]

    failed = client.delete(f"/instances/{inst_id}")
    assert failed.status_code == 502
    assert client.get(f"/instances/{inst_id}").json()["status"] == "Error"

    forced = client.delete(f"/instances/{inst_id}?force=true")
    assert forced.status_code == 200
    assert forced.json()["status"] == "Terminated"


def test_force_terminate_still_tries_the_hypervisor_first(client):
    """Force ignores a failure; it does not skip the attempt.

    Skipping the destroy would leak a VM on every forced terminate, including
    the overwhelming majority where the hypervisor is perfectly healthy.
    """
    inst_id = client.post("/instances", json={"name": "polite", "flavor": "small"}).json()["id"]

    assert client.delete(f"/instances/{inst_id}?force=true").status_code == 200
    assert ("destroy", "polite") in client.fake.calls  # type: ignore[attr-defined]


def test_ssh_key_endpoint_exposes_the_private_key_path(client):
    """Copy SSH needs `-i <key>`; VMs trust the orchestrator key and nothing else."""
    body = client.get("/ssh-key").json()

    assert body["private_key_path"]
    assert body["private_key_path"].endswith("id_ed25519")
    # The public half is what goes into the guest, and must never be the path.
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert body["ssh_user"] == "iaas"
    # Original field name retained so existing consumers keep working.
    assert body["key_path"] == body["private_key_path"]
