"""A minimal QMP client and framebuffer helpers, shared by the diagnostics in
this directory that have to drive a running QEMU guest rather than just launch
one.

Not a general QMP library: it implements exactly the handshake, one-command
round trip, and screendump polling that ``reset_measure.py`` needs, in the
same style as the private ``Qmp`` class in ``ab_measure.py`` (kept separate
rather than imported from there, since ``ab_measure.py`` is a working,
independently-shipped tool and this project avoids reaching into one script
from another for a handful of lines — see ``tools/README.md``).
"""
from __future__ import annotations

import json
import pathlib
import socket
import time


class Qmp:
    """One QMP connection: capabilities-negotiated, one client at a time.

    QEMU's QMP chardev serves a single client — see
    ``QemuEngine._liveness``'s docstring for the incident a second, permanent
    connection caused in the product itself. Diagnostics that use this class
    are expected to open one connection, do their work, and close it.
    """

    def __init__(self, port: int, host: str = "127.0.0.1", timeout: float = 10.0) -> None:
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.io = self.sock.makefile("rwb")
        self._read()  # greeting
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self.io.readline()
            if not line:
                raise RuntimeError("QMP connection closed")
            msg = json.loads(line)
            if "event" not in msg:
                return msg

    def cmd(self, execute: str, **args) -> dict:
        payload = {"execute": execute}
        if args:
            payload["arguments"] = args
        self.io.write((json.dumps(payload) + "\n").encode())
        self.io.flush()
        reply = self._read()
        if "error" in reply:
            raise RuntimeError(f"QMP {execute} failed: {reply['error']}")
        return reply

    def screendump(self, path: pathlib.Path) -> pathlib.Path:
        if path.exists():
            path.unlink()
        self.cmd("screendump", filename=str(path))
        for _ in range(50):
            if path.exists() and path.stat().st_size:
                time.sleep(0.3)  # QEMU writes the file after the reply lands
                return path
            time.sleep(0.2)
        raise RuntimeError("screendump produced nothing")

    def close(self) -> None:
        try:
            self.io.close()
            self.sock.close()
        except Exception:
            pass


def read_ppm(path: pathlib.Path) -> tuple[int, int, bytes]:
    """Parse a P6 PPM into (width, height, raw RGB bytes)."""
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 ppm")
    fields: list[int] = []
    idx = 2
    while len(fields) < 3:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":
            while data[idx:idx + 1] not in (b"\n", b""):
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        fields.append(int(data[start:idx]))
    return fields[0], fields[1], data[idx + 1:]


def frame_stats(path: pathlib.Path) -> tuple[int, str]:
    """(distinct colours, sha256[:12] of the raw frame) for one screendump.

    Colour count is what ``reboot_watchdog.py`` and this project's other
    diagnostics use to tell a BIOS/text-mode screen (SeaBIOS measured at ~2)
    from a graphical one (every graphical stage measured in this project's
    Windows-guest work: 12+). The hash is a cheap way to tell "the same frame,
    unchanged" from "a different frame with a similar colour count" — a
    blinking text-mode cursor toggles between two frames of identical colour
    count, which a colour-count-only comparison would misread as movement.
    """
    _w, _h, px = read_ppm(path)
    seen: set[bytes] = set()
    for i in range(0, len(px) - 2, 3):
        seen.add(px[i:i + 3])
    import hashlib
    digest = hashlib.sha256(px).hexdigest()[:12]
    return len(seen), digest
