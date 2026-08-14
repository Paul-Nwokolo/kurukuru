"""
Minimal QMP (QEMU Machine Protocol) client.

QMP is newline-delimited JSON over a socket. The handshake is fixed: the server
greets with ``{"QMP": {...}}``, the client answers ``qmp_capabilities``, and
from then on every ``{"execute": ...}`` gets exactly one ``{"return": ...}`` or
``{"error": ...}``. Asynchronous *events* (``{"event": "SHUTDOWN", ...}``) can
be interleaved at any point and are skipped while waiting for a reply.

Hand-rolled rather than using the ``qemu.qmp`` PyPI package: that package is
asyncio-first, and every caller here lives in a synchronous FastAPI background
task on Windows. ~120 lines of blocking sockets beats an event-loop bridge.
"""

from __future__ import annotations

import json
import logging
import socket

logger = logging.getLogger("iaas.qemu.qmp")

_DEFAULT_TIMEOUT = 10.0

#: Single phrasing for "the peer went away", so callers that tolerate it (the
#: `quit` command, which races QEMU's own exit) can match on one string.
CONNECTION_CLOSED = "QMP connection closed by QEMU"


class QmpError(Exception):
    """QMP connection failed, timed out, or the command returned an error."""


class QmpClient:
    """Blocking QMP client for one VM's control socket.

    Use as a context manager::

        with QmpClient("127.0.0.1", 4400) as qmp:
            status = qmp.execute("query-status")
    """

    def __init__(self, host: str, port: int, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buffer = b""

    # ---- lifecycle -------------------------------------------------------- #
    def connect(self) -> QmpClient:
        """Open the socket and complete the capabilities handshake."""
        try:
            self._sock = socket.create_connection((self._host, self._port), self._timeout)
            self._sock.settimeout(self._timeout)
        except OSError as exc:
            raise QmpError(f"Cannot connect to QMP at {self._host}:{self._port}: {exc}") from exc

        greeting = self._read_message()
        if "QMP" not in greeting:
            raise QmpError(f"Unexpected QMP greeting: {greeting!r}")
        # Until capabilities are negotiated the server rejects every command.
        self.execute("qmp_capabilities")
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:  # pragma: no cover - best effort
                pass
            self._sock = None

    def __enter__(self) -> QmpClient:
        return self.connect()

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- protocol --------------------------------------------------------- #
    def execute(self, command: str, **arguments: object) -> dict:
        """Run one QMP command and return its ``return`` payload."""
        if self._sock is None:
            raise QmpError("QMP client is not connected")

        request: dict[str, object] = {"execute": command}
        if arguments:
            request["arguments"] = arguments
        payload = (json.dumps(request) + "\r\n").encode("utf-8")
        try:
            self._sock.sendall(payload)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:
            raise QmpError(CONNECTION_CLOSED) from exc
        except OSError as exc:
            raise QmpError(f"QMP send of '{command}' failed: {exc}") from exc

        while True:
            message = self._read_message()
            if "return" in message:
                result = message["return"]
                return result if isinstance(result, dict) else {"return": result}
            if "error" in message:
                error = message["error"]
                raise QmpError(
                    f"QMP command '{command}' failed: "
                    f"{error.get('class', '?')}: {error.get('desc', error)}"
                )
            # Events and the (already consumed) greeting are not replies.
            logger.debug("QMP async message while awaiting '%s': %s", command, message)

    def _read_message(self) -> dict:
        """Read one newline-delimited JSON object from the socket."""
        if self._sock is None:
            raise QmpError("QMP client is not connected")
        while b"\n" not in self._buffer:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as exc:
                raise QmpError(
                    f"QMP read timed out after {self._timeout}s "
                    f"({self._host}:{self._port})"
                ) from exc
            except (ConnectionResetError, ConnectionAbortedError) as exc:
                # An exiting QEMU drops the socket abortively rather than
                # closing it cleanly — on Windows that surfaces as WinError
                # 10053/10054, not EOF. Both mean the same thing, and callers
                # (notably `quit`) must be able to tell them apart from a real
                # protocol failure.
                raise QmpError(CONNECTION_CLOSED) from exc
            except OSError as exc:
                raise QmpError(f"QMP read failed: {exc}") from exc
            if not chunk:
                raise QmpError(CONNECTION_CLOSED)
            self._buffer += chunk

        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QmpError(f"Malformed QMP message {line!r}: {exc}") from exc
        if not isinstance(message, dict):
            raise QmpError(f"Unexpected QMP message shape: {message!r}")
        return message


# --------------------------------------------------------------------------- #
# Convenience wrappers — the only three commands Phase 5 needs
# --------------------------------------------------------------------------- #
def query_status(host: str, port: int, timeout: float = _DEFAULT_TIMEOUT) -> str:
    """Return the guest run-state (``"running"``, ``"paused"``, ...)."""
    with QmpClient(host, port, timeout) as qmp:
        return str(qmp.execute("query-status").get("status", "unknown"))


def is_responsive(host: str, port: int, timeout: float = 3.0) -> bool:
    """True iff a QMP handshake + ``query-status`` succeeds. Never raises."""
    try:
        query_status(host, port, timeout)
        return True
    except QmpError:
        return False


def system_powerdown(host: str, port: int, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Send an ACPI power button press — the graceful shutdown path."""
    with QmpClient(host, port, timeout) as qmp:
        qmp.execute("system_powerdown")


def quit_vm(host: str, port: int, timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Ask QEMU to exit immediately (destroy path; no guest cooperation)."""
    with QmpClient(host, port, timeout) as qmp:
        try:
            qmp.execute("quit")
        except QmpError as exc:
            # QEMU frequently tears the socket down before writing a reply to
            # 'quit' — that is success, not failure.
            if CONNECTION_CLOSED not in str(exc):
                raise


def hostfwd_add(host: str, port: int, netdev: str, spec: str,
                timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Add a host port forward to a *running* guest.

    Routed through ``human-monitor-command`` because ``hostfwd_add`` has no
    native QMP equivalent — it is HMP-only in every QEMU release to date.

    HMP reports failure in its *output* rather than as a QMP error, so the
    string has to be inspected: a collision comes back as "Could not set up
    host forwarding rule ..." with a success status. Treating that as success
    would leave the database claiming a forward that does not exist.
    """
    with QmpClient(host, port, timeout) as qmp:
        result = qmp.execute(
            "human-monitor-command", **{"command-line": f"hostfwd_add {netdev} {spec}"}
        )
    _raise_on_hmp_error(result, spec)


def hostfwd_remove(host: str, port: int, netdev: str, spec: str,
                   timeout: float = _DEFAULT_TIMEOUT) -> None:
    """Remove a host port forward from a running guest.

    ``spec`` here omits the guest half — QEMU matches on the host side only,
    so ``tcp:127.0.0.1:8080`` is the whole key.
    """
    with QmpClient(host, port, timeout) as qmp:
        result = qmp.execute(
            "human-monitor-command", **{"command-line": f"hostfwd_remove {netdev} {spec}"}
        )
    _raise_on_hmp_error(result, spec)


def _raise_on_hmp_error(result: dict, spec: str) -> None:
    output = (result.get("return") or "").strip()
    lowered = output.lower()
    if "could not" in lowered or "invalid" in lowered or "not found" in lowered:
        raise QmpError(f"QEMU refused the port forward '{spec}': {output}")
