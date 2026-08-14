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
from collections.abc import Iterable

logger = logging.getLogger("iaas.qemu.ports")

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
