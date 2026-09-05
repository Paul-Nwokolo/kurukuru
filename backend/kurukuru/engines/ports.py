"""
Host port allocation for QEMU instances.

Each VM needs three host ports pinned for its whole life: the SSH forward
(``hostfwd``), the QMP control socket, and the VNC display. They are chosen
once at provision time and persisted, so "Copy SSH" keeps working across
stop/start and backend restarts.

Two things must be true of a candidate port:
  1. nothing is currently listening on it — proved by actually binding, which
     is the only check that isn't a race with the OS, and
  2. it isn't already reserved by another instance, including *stopped* ones
     whose ports are unbound but still spoken for.
"""

from __future__ import annotations

import logging
import socket
import time
from collections.abc import Iterable

logger = logging.getLogger("kurukuru.qemu.ports")

_BIND_HOST = "127.0.0.1"


class PortAllocationError(Exception):
    """No free port was available in the requested range."""


def is_port_free(port: int, host: str = _BIND_HOST) -> bool:
    """True iff ``port`` can be bound right now on ``host``.

    SO_REUSEADDR is deliberately *not* set: we want the bind to fail if anything
    else holds the port, which is exactly what a plain bind reports.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def wait_for_port_free(
    port: int,
    host: str = _BIND_HOST,
    *,
    timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> bool:
    """Poll :func:`is_port_free` until it says yes, or ``timeout`` elapses.

    Exists for one specific scenario, confirmed by forcing it directly (bind a
    port in a child process, kill the child the same way
    ``QemuEngine._force_off`` does, then hammer-rebind with no delay at all):
    the OS can still refuse a bind for a short window *after* the killed
    process is confirmed gone by ``pid_alive()`` — every forced trial saw the
    first several rebind attempts fail before one finally succeeded. A single
    :func:`is_port_free` check right after a forced kill cannot tell that
    transient window apart from a genuinely different process now owning the
    port. Retrying for a short, bounded window resolves the former without
    meaningfully delaying detection of the latter: a real conflict still
    reports not-free after ``timeout``.

    Not a general "wait for anything" helper — it exists only to bridge this
    one measured race, which is why the default timeout is short.
    """
    deadline = time.monotonic() + timeout
    while True:
        if is_port_free(port, host):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)


def allocate_port(
    low: int,
    high: int,
    *,
    reserved: Iterable[int] = (),
    host: str = _BIND_HOST,
) -> int:
    """Return the lowest free port in ``[low, high]`` not in ``reserved``.

    Deterministic (lowest-first) rather than random so a fresh host hands out
    2200, 2201, ... — predictable ports make the live SSH story friendlier.
    """
    if low > high:
        raise PortAllocationError(f"Invalid port range {low}-{high}")
    taken = set(reserved)
    for port in range(low, high + 1):
        if port in taken:
            continue
        if is_port_free(port, host):
            return port
    raise PortAllocationError(
        f"No free port in range {low}-{high} "
        f"({len(taken)} reserved by existing instances)"
    )
