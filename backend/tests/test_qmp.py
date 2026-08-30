"""
QMP client tests against a fake in-process QMP server.

A real socket server (on an ephemeral loopback port) rather than a mocked
``socket`` object: the interesting failure modes are protocol-level — a reply
split across TCP segments, an event arriving before the reply, the peer hanging
up mid-command — and none of those are reproducible against a mock.

The fake speaks the same script every real QEMU does: greeting, then one reply
per command.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from kurukuru.engines.qmp import (
    QmpClient,
    QmpError,
    is_responsive,
    query_status,
    screendump,
    system_powerdown,
)

_GREETING = {"QMP": {"version": {"qemu": {"major": 10, "minor": 0}}, "capabilities": []}}


class FakeQmpServer:
    """Single-connection QMP server driven by a per-command reply function."""

    def __init__(
        self,
        responder: Callable[[dict], list[bytes]],
        *,
        greeting: bytes | None = None,
        hang_up_after: int | None = None,
    ) -> None:
        self._responder = responder
        self._greeting = greeting if greeting is not None else json.dumps(_GREETING).encode() + b"\r\n"
        self._hang_up_after = hang_up_after
        self.received: list[dict] = []
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeQmpServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            conn.sendall(self._greeting)
            buffer = b""
            handled = 0
            while True:
                if self._hang_up_after is not None and handled >= self._hang_up_after:
                    return
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if not line.strip():
                        continue
                    request = json.loads(line)
                    self.received.append(request)
                    handled += 1
                    for payload in self._responder(request):
                        conn.sendall(payload)
                    if self._hang_up_after is not None and handled >= self._hang_up_after:
                        return


def _reply(payload: dict) -> bytes:
    return json.dumps(payload).encode() + b"\r\n"


def _ok(_request: dict) -> list[bytes]:
    return [_reply({"return": {}})]


# --------------------------------------------------------------------------- #
# Handshake
# --------------------------------------------------------------------------- #
def test_connect_negotiates_capabilities_first():
    with FakeQmpServer(_ok) as server:
        with QmpClient("127.0.0.1", server.port):
            pass
    # Nothing else is legal until qmp_capabilities has been accepted.
    assert server.received[0] == {"execute": "qmp_capabilities"}


def test_query_status_returns_the_run_state():
    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "query-status":
            return [_reply({"return": {"status": "running", "running": True}})]
        return _ok(request)

    with FakeQmpServer(responder) as server:
        assert query_status("127.0.0.1", server.port) == "running"


def test_arguments_are_sent_when_provided():
    with FakeQmpServer(_ok) as server:
        with QmpClient("127.0.0.1", server.port) as qmp:
            qmp.execute("device_del", id="virtio-net")
    assert server.received[-1] == {
        "execute": "device_del",
        "arguments": {"id": "virtio-net"},
    }


def test_system_powerdown_is_issued():
    with FakeQmpServer(_ok) as server:
        system_powerdown("127.0.0.1", server.port)
    assert [r["execute"] for r in server.received] == ["qmp_capabilities", "system_powerdown"]


# --------------------------------------------------------------------------- #
# Protocol edge cases
# --------------------------------------------------------------------------- #
def test_events_before_the_reply_are_skipped():
    """QEMU interleaves async events with replies; they must not be mistaken
    for the answer to the command in flight."""

    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "query-status":
            return [
                _reply({"event": "RESUME", "timestamp": {"seconds": 1, "microseconds": 0}}),
                _reply({"event": "NIC_RX_FILTER_CHANGED", "data": {}}),
                _reply({"return": {"status": "running"}}),
            ]
        return _ok(request)

    with FakeQmpServer(responder) as server:
        assert query_status("127.0.0.1", server.port) == "running"


def test_reply_split_across_packets_is_reassembled():
    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "query-status":
            blob = _reply({"return": {"status": "paused"}})
            return [blob[:9], blob[9:]]  # arbitrary mid-JSON split
        return _ok(request)

    with FakeQmpServer(responder) as server:
        assert query_status("127.0.0.1", server.port) == "paused"


def test_buffered_event_from_a_previous_packet_is_skipped():
    """One recv() can deliver a reply *and* a trailing event.

    The event is left in the buffer and must be discarded when the next command
    reads its reply — otherwise every command after an event returns the
    previous command's leftovers.
    """

    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "qmp_capabilities":
            # Handshake reply and an unsolicited event, in a single packet.
            return [_reply({"return": {}}) + _reply({"event": "STOP", "data": {}})]
        return [_reply({"return": {"status": "paused"}})]

    with FakeQmpServer(responder) as server:
        with QmpClient("127.0.0.1", server.port) as qmp:
            assert qmp.execute("query-status") == {"status": "paused"}


# --------------------------------------------------------------------------- #
# Failure modes
# --------------------------------------------------------------------------- #
def test_qmp_error_reply_raises_with_the_description():
    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "query-status":
            return [_reply({"error": {"class": "CommandNotFound", "desc": "no such command"}})]
        return _ok(request)

    with FakeQmpServer(responder) as server:
        with pytest.raises(QmpError, match="no such command"):
            query_status("127.0.0.1", server.port)


def test_connection_refused_raises():
    # Bind and immediately release a port so nothing is listening on it.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    with pytest.raises(QmpError, match="Cannot connect"):
        query_status("127.0.0.1", port)


def test_peer_hangup_mid_command_raises():
    with FakeQmpServer(_ok, hang_up_after=1) as server:
        with pytest.raises(QmpError, match="closed by QEMU"):
            query_status("127.0.0.1", server.port)


def test_malformed_greeting_is_rejected():
    with FakeQmpServer(_ok, greeting=b'{"not":"a greeting"}\r\n') as server:
        with pytest.raises(QmpError, match="greeting"):
            QmpClient("127.0.0.1", server.port).connect()


def test_non_json_message_is_rejected():
    with FakeQmpServer(_ok, greeting=b"<<<not json>>>\r\n") as server:
        with pytest.raises(QmpError, match="Malformed"):
            QmpClient("127.0.0.1", server.port).connect()


def test_execute_before_connect_raises():
    with pytest.raises(QmpError, match="not connected"):
        QmpClient("127.0.0.1", 1).execute("query-status")


# --------------------------------------------------------------------------- #
# is_responsive: the liveness probe used by the reconciler
# --------------------------------------------------------------------------- #
def test_is_responsive_true_against_a_live_server():
    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "query-status":
            return [_reply({"return": {"status": "running"}})]
        return _ok(request)

    with FakeQmpServer(responder) as server:
        assert is_responsive("127.0.0.1", server.port) is True


def test_is_responsive_false_when_nothing_listens():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    assert is_responsive("127.0.0.1", port, timeout=1.0) is False


# --------------------------------------------------------------------------- #
# screendump: the reboot watchdog's only diagnostic (kurukuru/reboot_watchdog.py)
# --------------------------------------------------------------------------- #
def test_screendump_waits_for_the_file_qemu_writes(tmp_path: Path):
    """The QMP reply says nothing about the write landing — real QEMU performs
    it as a side effect. The fake mirrors that: it answers immediately and
    writes the file itself, after a short delay, exactly like a real VM."""
    target = tmp_path / "frame.ppm"

    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "screendump":
            threading.Timer(
                0.2, lambda: target.write_bytes(b"P6\n1 1\n255\n\x00\x00\x00")
            ).start()
            return [_reply({"return": {}})]
        return _ok(request)

    with FakeQmpServer(responder) as server:
        result = screendump("127.0.0.1", server.port, target, timeout=3.0)
    assert result == target
    assert target.read_bytes().startswith(b"P6")


def test_screendump_raises_if_the_file_never_appears(tmp_path: Path):
    with FakeQmpServer(_ok) as server:
        with pytest.raises(QmpError, match="produced nothing"):
            screendump("127.0.0.1", server.port, tmp_path / "never.ppm", timeout=0.5)


def test_screendump_discards_a_stale_file_from_a_previous_call(tmp_path: Path):
    """A leftover frame from an earlier sample must not be mistaken for a new
    one if this call's write is slow — stale-frame risk is exactly what broke
    an early version of the reboot watchdog's own diagnostic harness."""
    target = tmp_path / "frame.ppm"
    target.write_bytes(b"P6\n1 1\n255\n\xff\xff\xff")  # stale "old" frame

    def responder(request: dict) -> list[bytes]:
        if request["execute"] == "screendump":
            return [_reply({"return": {}})]  # never actually writes
        return _ok(request)

    with FakeQmpServer(responder) as server:
        with pytest.raises(QmpError, match="produced nothing"):
            screendump("127.0.0.1", server.port, target, timeout=0.5)
    assert not target.exists()  # the stale file was removed, not left to lie
