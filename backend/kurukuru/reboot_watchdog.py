"""
A heuristic workaround for an upstream QEMU/WHPX defect, not a general feature.

**What this exists for.** QEMU's `system_reset` under WHPX deterministically
fails to bring a Windows guest back up after an in-process reboot — the guest
is left stuck at SeaBIOS's boot-device-probing prompt forever, with the QEMU
process itself alive and its QMP socket fully responsive throughout. Measured
at 36/36 across every chipset, RTC/HPET, Hyper-V-enlightenment, and CD-ejection
combination tried, and reproduced identically on an officially tagged QEMU
release, not just a development snapshot — see docs/WINDOWS.md for the
measurements and <UPSTREAM ISSUE URL — fill in once filed> for the report. A
fresh QEMU process against the same disk state always works, which is the
entire mechanism this module leans on: restart the process, not the guest.

**Why this is a heuristic and not a fix.** Nothing here detects the actual
reboot event — QEMU's QMP chardev serves one client at a time (see
``QemuEngine._liveness``'s docstring for the incident that constraint already
caused once), so holding a second, persistent connection open just to watch
for a `RESET` event risks reintroducing that exact class of bug. Instead this
watches the *symptom*: a guest that has shown nothing but a static, low-colour
(SeaBIOS-text-mode) framebuffer for far longer than any legitimate boot has
ever taken in this project's measurements (every successful boot observed,
across dozens of runs, landed under 40 seconds; the default threshold below
gives that roughly 8x headroom). That is an inference, not a certainty.

**Named misfire conditions — read before changing the threshold or scope.**

* **A Windows guest legitimately showing a static or near-black screen for
  longer than the threshold** would be restarted regardless of whether
  anything is actually wrong. A full disk-check (`chkdsk` at boot), a very
  slow storage backend, or a guest deliberately parked at a black screen by
  its own configuration could all trigger this. There is no way to
  distinguish "slow" from "stuck" from the framebuffer alone.
* **Scope is `guest_os == "windows"` only, deliberately.** A headless Linux
  server sitting at an idle console (blanked, or just quiet) is a *normal*
  static-low-colour steady state, not a symptom — auto-restarting one because
  it isn't drawing anything would be a serious bug, not a helpful recovery.
* **The per-instance tracking state lives in process memory only.** A backend
  restart forgets how long an instance has looked stuck and starts the clock
  over. This is an accepted gap, not an oversight — persisting it would add
  real complexity for a watchdog that is meant to be temporary.
* **At most one automatic restart per cooldown window, by design** (see
  ``Settings.windows_reboot_watchdog_cooldown_seconds``). If the guest is
  stuck again when the cooldown allows another attempt, this does not loop
  indefinitely — the instance is marked ``Error`` with an explanation instead,
  and a human has to look at it.

**Removability.** Everything this defect's workaround touches is confined to:
this file, the four `windows_reboot_watchdog_*` settings in `config.py`, the
`EventKind.AUTO_RESTARTED` value, `windows_reboot_watchdog_pass` in
`routers/instances.py` (clearly delimited there), its scheduling in `main.py`,
and the frontend's auto-reconnect behaviour in `ConsoleModal.tsx`. If the
upstream issue is fixed and this project moves its minimum QEMU version past
the fix, all of the above should come out together, in one commit — this
docstring is the checklist for that commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger("kurukuru.reboot_watchdog")


# --------------------------------------------------------------------------- #
# Framebuffer classification — no third-party imaging library required.
#
# Adapted from the same minimal PPM (P6) parser used by tools/ab_measure.py to
# characterise this exact defect during Windows-guest investigation. Kept
# separate rather than imported from tools/, which is diagnostic scripts, not
# library code this package should depend on.
# --------------------------------------------------------------------------- #
def _read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError(f"{path} is not a binary (P6) PPM")
    fields: list[int] = []
    idx = 2
    while len(fields) < 3:
        while idx < len(data) and data[idx : idx + 1].isspace():
            idx += 1
        if data[idx : idx + 1] == b"#":
            while idx < len(data) and data[idx : idx + 1] not in (b"\n", b""):
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx : idx + 1].isspace():
            idx += 1
        fields.append(int(data[start:idx]))
    return fields[0], fields[1], data[idx + 1 :]


def colour_count(path: Path) -> int:
    """Distinct RGB colours in a screendump PPM.

    The discriminator this whole module rests on: measured across this
    project's Windows-guest investigation, SeaBIOS's text-mode boot prompt
    renders as ~2 colours (foreground text, black background), while every
    graphical stage observed — the boot logo, Setup, a desktop — has shown
    12 colours or more. See ``Settings.windows_reboot_watchdog_colour_threshold``.
    """
    _width, _height, pixels = _read_ppm(path)
    seen: set[bytes] = set()
    for i in range(0, len(pixels) - 2, 3):
        seen.add(pixels[i : i + 3])
    return len(seen)


# --------------------------------------------------------------------------- #
# Per-instance state machine — pure, no I/O, so it is exercised directly by
# tests without a QMP socket or a database in sight.
# --------------------------------------------------------------------------- #
class Action:
    """What ``advance`` decided to do. Exactly one of these per call."""

    NOTHING = "nothing"
    RESTART = "restart"
    GIVE_UP = "give_up"


@dataclass
class InstanceWatch:
    """Tracking state for one instance, held only in process memory.

    ``low_since`` is the start of the *current uninterrupted* run of
    low-colour samples — reset to ``None`` the moment a graphical frame is
    seen, so a guest that legitimately takes 35 seconds to reach Setup and a
    guest that has been stuck for 35 seconds look identical until the
    threshold is actually crossed. That is deliberate: this does not try to
    tell "slow" from "stuck" any earlier than it has to.
    """

    low_since: datetime | None = None
    last_restart_at: datetime | None = None
    alerted_this_window: bool = False

    def advance(
        self,
        *,
        colours: int,
        now: datetime,
        colour_threshold: int,
        stuck_seconds: float,
        cooldown_seconds: float,
    ) -> str:
        if colours > colour_threshold:
            self.low_since = None
            return Action.NOTHING

        if self.low_since is None:
            self.low_since = now
            return Action.NOTHING

        stuck_for = (now - self.low_since).total_seconds()
        if stuck_for < stuck_seconds:
            return Action.NOTHING

        in_cooldown = (
            self.last_restart_at is not None
            and (now - self.last_restart_at).total_seconds() < cooldown_seconds
        )
        if not in_cooldown:
            self.last_restart_at = now
            self.low_since = now  # a fresh window starts counting from the restart
            self.alerted_this_window = False
            return Action.RESTART

        if not self.alerted_this_window:
            self.alerted_this_window = True
            return Action.GIVE_UP
        return Action.NOTHING


class WatchdogRegistry:
    """Thread-safe home for every instance's :class:`InstanceWatch`.

    A plain dict would do for correctness — the reconcile loop already runs on
    one worker thread at a time — but the lock costs nothing and matches the
    precedent `routers.instances._in_flight_lock` already sets for exactly
    this kind of shared, process-lifetime state.
    """

    def __init__(self) -> None:
        self._watches: dict[str, InstanceWatch] = {}
        self._lock = Lock()

    def get(self, instance_id: str) -> InstanceWatch:
        with self._lock:
            watch = self._watches.get(instance_id)
            if watch is None:
                watch = self._watches[instance_id] = InstanceWatch()
            return watch

    def known_ids(self) -> set[str]:
        with self._lock:
            return set(self._watches)

    def discard(self, instance_id: str) -> None:
        """Drop tracking for an instance that is no longer running.

        Called once an instance leaves the Windows/Running set the watchdog
        cares about, so a VM that stops, is terminated, or switches engines
        does not carry a stale `low_since` into whatever it becomes next.
        """
        with self._lock:
            self._watches.pop(instance_id, None)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
