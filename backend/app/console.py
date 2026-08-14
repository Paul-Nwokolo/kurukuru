"""
VNC-over-WebSocket bridge for the in-browser console.

QEMU exposes a raw RFB (VNC) socket; browsers can only open WebSockets. The
usual answer is a separate ``websockify`` process, but that is another binary to
ship, supervise and firewall — so the backend does the bridging itself: accept
the WebSocket, dial the VM's loopback VNC port, and pump bytes both ways.

Deliberately *not* an RFB implementation. Not one byte is parsed, framed or
rewritten; noVNC in the browser talks to QEMU as if it held the socket directly.
That keeps this module immune to RFB version differences, encodings and
authentication schemes — QEMU and noVNC negotiate all of it end to end.

Security note: the VNC socket is bound to 127.0.0.1 by the launch args, so this
proxy is the *only* path to a VM's framebuffer. That bind is asserted by tests;
if it ever became 0.0.0.0 every VM's screen would be on the LAN unauthenticated.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

logger = logging.getLogger("iaas.console")

#: Read chunk for the VM->browser direction. Bounded on purpose: a VM redrawing
#: its whole screen can produce megabytes, and an unbounded read would buffer it
#: all in memory if the browser stalls. With a bounded read, `await send_bytes`
#: applies backpressure and the TCP window does the rest.
_CHUNK_BYTES = 64 * 1024

_CONNECT_TIMEOUT_SECONDS = 5.0

# Application close codes (RFC 6455 reserves 4000-4999 for applications). The UI
# maps these to human explanations, so they are part of the API contract.
CLOSE_NOT_FOUND = 4404
CLOSE_CONFLICT = 4409
CLOSE_VNC_UNAVAILABLE = 4502
CLOSE_NORMAL = 1000


async def bridge_websocket_to_vnc(
    websocket: WebSocket,
    host: str,
    port: int,
    *,
    label: str = "?",
) -> None:
    """Pump bytes between an accepted WebSocket and a VNC TCP socket.

    Returns once either side closes. The WebSocket is closed before returning;
    the caller does not need to close it again.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=_CONNECT_TIMEOUT_SECONDS
        )
    except (OSError, asyncio.TimeoutError) as exc:
        logger.warning("Console '%s': cannot reach VNC at %s:%d: %s", label, host, port, exc)
        await _safe_close(
            websocket, CLOSE_VNC_UNAVAILABLE, "VNC socket unreachable — the VM may have stopped"
        )
        return

    logger.info("Console '%s': bridging to %s:%d", label, host, port)

    async def browser_to_vm() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            # noVNC sends binary, but tolerate text frames rather than dying on
            # a proxy that transcodes them.
            data = message.get("bytes")
            if data is None:
                text = message.get("text")
                if text is None:
                    continue
                data = text.encode("utf-8", errors="ignore")
            writer.write(data)
            await writer.drain()

    async def vm_to_browser() -> None:
        while True:
            data = await reader.read(_CHUNK_BYTES)
            if not data:  # QEMU closed the socket (VM stopped or quit)
                return
            await websocket.send_bytes(data)

    tasks = [asyncio.create_task(browser_to_vm()), asyncio.create_task(vm_to_browser())]
    try:
        # Whichever direction ends first ends the session; the other is torn down.
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                logger.info("Console '%s' ended: %s: %s", label, type(exc).__name__, exc)
    finally:
        writer.close()
        with suppress(OSError, asyncio.TimeoutError):
            await writer.wait_closed()
        await _safe_close(websocket, CLOSE_NORMAL, "console closed")
        logger.info("Console '%s': closed", label)


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    """Close a WebSocket, tolerating a client that already went away."""
    if websocket.client_state is WebSocketState.DISCONNECTED:
        return
    with suppress(RuntimeError, OSError):
        await websocket.close(code=code, reason=reason)
