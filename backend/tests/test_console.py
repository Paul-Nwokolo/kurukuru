"""
Tests for the VNC-over-WebSocket console bridge.

The bridge is byte-transparent by design, so the tests assert exactly that:
whatever a fake "QEMU" writes to its VNC socket arrives at the browser
unaltered, and vice versa. A real loopback TCP server stands in for QEMU —
mocking the socket would prove nothing about a pump whose entire job is moving
bytes between two real transports.

The refusal paths matter as much as the happy one: a console that fails with a
bare "connection closed" leaves the UI with nothing to tell the user.
"""

from __future__ import annotations

import socket
import threading
from contextlib import suppress

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

import kurukuru.engines as engines_module
from tests.conftest import authenticate_test_client, redirect_db_engines
import kurukuru.events as events_module
import kurukuru.routers.instances as instances_module
from kurukuru.console import CLOSE_CONFLICT, CLOSE_NOT_FOUND, CLOSE_VNC_UNAVAILABLE
from kurukuru.database import get_session
from kurukuru.engines import EngineRegistry, get_engine_registry
from kurukuru.main import app
from kurukuru.models import Instance, InstanceStatus

from tests.test_instances_api import FakeQemuEngine


class FakeVncServer:
    """A loopback TCP server that stands in for QEMU's -vnc socket.

    Echoes a greeting on connect (like RFB's version handshake) and then echoes
    back everything it receives, so both directions can be asserted.
    """

    def __init__(self, greeting: bytes = b"RFB 003.008\n") -> None:
        self._greeting = greeting
        self.received = bytearray()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._conn: socket.socket | None = None

    def __enter__(self) -> FakeVncServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Go away the way a stopped VM does — which needs more than close().

        ``shutdown()`` first, and only on POSIX does it matter. Closing a
        socket that another thread is blocked in ``recv()`` on does not wake
        that thread there: the descriptor is dropped, but the socket itself
        survives behind the blocked call and **no FIN is sent**, so the peer
        waits forever for an EOF that never comes. Windows instead resets the
        connection on close, which is why this stood up on Windows for nine
        phases and hung the suite on the first Linux run.

        Measured, both directions:

            close() only         Linux: no EOF   Windows: ECONNRESET
            shutdown() + close() Linux: EOF      —

        Real QEMU needs none of this: a process exiting closes its descriptors
        and the kernel sends FIN. It is only a *simulated* departure that has
        to be explicit about it.
        """
        if self._conn is not None:
            with suppress(OSError):  # already closed, or never connected
                self._conn.shutdown(socket.SHUT_RDWR)
        for sock in (self._conn, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._thread.join(timeout=3)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        self._conn = conn
        try:
            conn.sendall(self._greeting)
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                self.received.extend(data)
                conn.sendall(b"echo:" + data)
        except OSError:
            return


@pytest.fixture()
def client(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    registry = EngineRegistry({"qemu": lambda: FakeQemuEngine()})

    def _session_override():
        with Session(test_engine) as s:
            yield s

    redirect_db_engines(monkeypatch, test_engine)
    monkeypatch.setattr(engines_module, "_registry", registry)
    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_engine_registry] = lambda: registry

    with TestClient(app) as c:
        c.db_engine = test_engine  # type: ignore[attr-defined]
        authenticate_test_client(c, test_engine)
        yield c

    app.dependency_overrides.clear()


def _console_url(client, instance_id: str) -> str:
    """The console URL with a freshly minted ticket.

    Phase 15 made the console ticket-authenticated: mint over authenticated
    HTTP, redeem once on connect. Tickets are single-use, so this is called per
    connection rather than hoisted into a fixture — a shared one would be spent
    by the first test that used it.
    """
    response = client.post(f"/instances/{instance_id}/console/ticket")
    assert response.status_code == 200, response.text
    return f"/instances/{instance_id}/console?ticket={response.json()['ticket']}"


def _add_row(client, **kwargs) -> str:
    defaults = {
        "name": "vm",
        "engine": "qemu",
        "status": InstanceStatus.RUNNING,
        "ip_address": "127.0.0.1",
        "vnc_port": 5900,
    }
    with Session(client.db_engine) as session:  # type: ignore[attr-defined]
        row = Instance(**{**defaults, **kwargs})
        session.add(row)
        session.commit()
        return row.id


# --------------------------------------------------------------------------- #
# Byte transparency
# --------------------------------------------------------------------------- #
def test_bytes_flow_from_vm_to_browser(client):
    """QEMU's RFB greeting must reach noVNC untouched — no framing, no rewrite."""
    with FakeVncServer() as vnc:
        row_id = _add_row(client, vnc_port=vnc.port)
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            assert ws.receive_bytes() == b"RFB 003.008\n"


def test_bytes_flow_from_browser_to_vm(client):
    with FakeVncServer() as vnc:
        row_id = _add_row(client, vnc_port=vnc.port)
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()  # greeting
            ws.send_bytes(b"RFB 003.008\n")
            assert ws.receive_bytes() == b"echo:RFB 003.008\n"
        assert bytes(vnc.received) == b"RFB 003.008\n"


def test_binary_payloads_survive_intact(client):
    """Framebuffer data is arbitrary bytes: NULs and high bytes must pass."""
    payload = bytes(range(256)) * 4
    with FakeVncServer() as vnc:
        row_id = _add_row(client, vnc_port=vnc.port)
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()
            ws.send_bytes(payload)
            received = b""
            while len(received) < len(payload) + 5:
                received += ws.receive_bytes()
        assert received == b"echo:" + payload
        assert bytes(vnc.received) == payload


def test_vm_closing_its_socket_ends_the_session(client):
    """Stopping a VM kills QEMU; the browser must see a clean close, not a hang."""
    vnc = FakeVncServer()
    vnc.__enter__()
    row_id = _add_row(client, vnc_port=vnc.port)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()  # greeting
            vnc.close()         # QEMU goes away
            while True:
                ws.receive_bytes()


# --------------------------------------------------------------------------- #
# Refusals — each carries a code the UI can explain
# --------------------------------------------------------------------------- #
def test_an_unknown_instance_does_not_reveal_that_it_is_unknown(client):
    """Authentication runs before the lookup, deliberately.

    This test used to assert CLOSE_NOT_FOUND for an unauthenticated connect,
    which was correct when the console was open to anyone. Now it would be an
    existence oracle: a caller with no ticket could learn which instance ids are
    real by watching "not found" turn into something else. It gets the same
    refusal as every other bad ticket.
    """
    from kurukuru.console import CLOSE_UNAUTHENTICATED

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/instances/nope/console") as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_UNAUTHENTICATED


def test_terminating_between_minting_and_connecting_is_a_conflict(client):
    """Terminating does not delete the row — rows outlive their VMs — so a
    ticket redeemed afterwards reaches the status check, not the lookup."""
    row_id = _add_row(client, vnc_port=5999)
    url = _console_url(client, row_id)
    client.delete(f"/instances/{row_id}")

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_CONFLICT


def test_not_found_is_still_reachable_with_a_valid_ticket(client):
    """The one path left to CLOSE_NOT_FOUND: the row is gone entirely, which
    only happens if something removed it out of band."""
    from sqlmodel import Session

    from kurukuru.models import Instance

    row_id = _add_row(client, vnc_port=5999)
    url = _console_url(client, row_id)
    with Session(client.db_engine) as session:
        session.delete(session.get(Instance, row_id))
        session.commit()

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(url) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_NOT_FOUND


def test_retired_engine_instance_is_refused(client):
    """A legacy Multipass row has no driver and no framebuffer behind it.

    The console must refuse with an explanation rather than trying to dial a
    VNC port the row may still be carrying from before the retirement.
    """
    row_id = _add_row(client, name="mp", engine="multipass", vnc_port=5999)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_CONFLICT


@pytest.mark.parametrize(
    "state", [InstanceStatus.STOPPED, InstanceStatus.PROVISIONING, InstanceStatus.TERMINATED]
)
def test_non_running_instance_is_refused(client, state):
    row_id = _add_row(client, status=state)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_CONFLICT


def test_missing_vnc_port_is_refused(client):
    row_id = _add_row(client, vnc_port=None)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_CONFLICT


def test_console_is_offered_for_every_accelerator(client):
    """The old rule ("WHPX means no console") blocked a case that works.

    An ISO guest that does a KMS modeset renders live on std VGA under WHPX —
    verified against Alpine — so refusing the console for accelerated VMs hid a
    working feature.
    """
    whpx_std = _add_row(client, name="whpx-std", accel="whpx", display="std")
    whpx_virtio = _add_row(client, name="whpx-virtio", accel="whpx", display="virtio")
    tcg_std = _add_row(client, name="tcg-std", accel="tcg", display="std")

    for row_id in (whpx_std, whpx_virtio, tcg_std):
        assert client.get(f"/instances/{row_id}").json()["console_supported"] is True


def test_console_is_withheld_until_the_accelerator_is_known(client):
    """Mid-provision there is no VNC socket to connect to yet."""
    pending = _add_row(client, name="pending", accel=None)
    assert client.get(f"/instances/{pending}").json()["console_supported"] is False


def test_console_is_never_offered_for_a_retired_engine(client):
    mp = _add_row(client, name="mp-vm", engine="multipass", accel=None)
    assert client.get(f"/instances/{mp}").json()["console_supported"] is False


def test_caveat_marks_only_text_mode_vga_under_hardware_acceleration(client):
    """The advisory replaces the old blanket block, and names the real cause."""
    rows = {
        ("whpx", "std"): _add_row(client, name="hw-std", accel="whpx", display="std"),
        ("whpx", "virtio"): _add_row(client, name="hw-virtio", accel="whpx", display="virtio"),
        ("tcg", "std"): _add_row(client, name="sw-std", accel="tcg", display="std"),
        ("tcg", "virtio"): _add_row(client, name="sw-virtio", accel="tcg", display="virtio"),
    }
    caveats = {
        combo: client.get(f"/instances/{rid}").json()["console_caveat"]
        for combo, rid in rows.items()
    }

    assert caveats[("whpx", "std")] is not None
    assert "text mode" in caveats[("whpx", "std")]
    assert caveats[("whpx", "virtio")] is None
    assert caveats[("tcg", "std")] is None
    assert caveats[("tcg", "virtio")] is None


def test_legacy_rows_without_a_display_are_treated_as_std(client):
    """Pre-Phase-7 rows recorded no display but all ran std VGA."""
    row = _add_row(client, name="legacy", accel="whpx", display=None)
    assert client.get(f"/instances/{row}").json()["console_caveat"] is not None


def test_dead_vnc_socket_reports_unavailable(client):
    """Row says Running but QEMU is gone — the UI needs to say so, not hang."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    row_id = _add_row(client, vnc_port=dead_port)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(_console_url(client, row_id)) as ws:
            ws.receive_bytes()
    assert exc.value.code == CLOSE_VNC_UNAVAILABLE
