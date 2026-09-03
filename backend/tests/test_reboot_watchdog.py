"""
Tests for the reboot-watchdog workaround's pure logic — no QMP socket, no
database, no event loop. See kurukuru/reboot_watchdog.py's module docstring
for what this exists to work around and its named misfire conditions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kurukuru.reboot_watchdog import (
    Action,
    InstanceWatch,
    WatchdogRegistry,
    colour_count,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _ppm(width: int, height: int, pixels: bytes) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode() + pixels


# --------------------------------------------------------------------------- #
# colour_count — the SeaBIOS-text-mode vs. graphical discriminator
# --------------------------------------------------------------------------- #
def test_colour_count_counts_distinct_rgb_triples(tmp_path: Path):
    path = tmp_path / "frame.ppm"
    # Four pixels, two distinct colours.
    path.write_bytes(_ppm(2, 2, b"\x00\x00\x00" * 3 + b"\xff\xff\xff"))
    assert colour_count(path) == 2


def test_colour_count_a_single_flat_colour_is_one(tmp_path: Path):
    path = tmp_path / "frame.ppm"
    path.write_bytes(_ppm(4, 4, b"\x10\x20\x30" * 16))
    assert colour_count(path) == 1


def test_colour_count_rejects_a_non_p6_file(tmp_path: Path):
    path = tmp_path / "frame.ppm"
    path.write_bytes(b"P3\nnot binary")
    with pytest.raises(ValueError, match="not a binary"):
        colour_count(path)


def test_colour_count_tolerates_a_comment_in_the_header(tmp_path: Path):
    path = tmp_path / "frame.ppm"
    path.write_bytes(b"P6\n# a comment\n2 1\n255\n" + b"\x01\x02\x03" * 2)
    assert colour_count(path) == 1


# --------------------------------------------------------------------------- #
# InstanceWatch.advance — the state machine that decides RESTART / GIVE_UP
# --------------------------------------------------------------------------- #
_KW = dict(colour_threshold=8, stuck_seconds=300.0, cooldown_seconds=3600.0)


def test_a_graphical_frame_never_starts_the_clock():
    watch = InstanceWatch()
    for minutes in range(0, 120, 10):
        action = watch.advance(colours=800, now=_EPOCH + timedelta(minutes=minutes), **_KW)
        assert action == Action.NOTHING
    assert watch.low_since is None


def test_a_low_frame_does_nothing_until_the_threshold_is_crossed():
    watch = InstanceWatch()
    assert watch.advance(colours=2, now=_EPOCH, **_KW) == Action.NOTHING
    # Just under the threshold: still nothing.
    almost = _EPOCH + timedelta(seconds=299)
    assert watch.advance(colours=2, now=almost, **_KW) == Action.NOTHING


def test_crossing_the_threshold_triggers_exactly_one_restart():
    watch = InstanceWatch()
    watch.advance(colours=2, now=_EPOCH, **_KW)
    past = _EPOCH + timedelta(seconds=301)
    assert watch.advance(colours=2, now=past, **_KW) == Action.RESTART
    # Immediately re-checking (still stuck, cooldown not yet elapsed) must not
    # restart again — this is the once-per-window guard.
    assert watch.advance(colours=2, now=past + timedelta(seconds=1), **_KW) == Action.NOTHING


def test_a_recovered_guest_resets_the_clock_after_a_near_miss():
    """A guest that is merely slow — not stuck — must not carry a partial
    low-colour streak into a later, unrelated slow moment."""
    watch = InstanceWatch()
    watch.advance(colours=2, now=_EPOCH, **_KW)
    watch.advance(colours=2, now=_EPOCH + timedelta(seconds=250), **_KW)
    # Recovers before the threshold.
    watch.advance(colours=800, now=_EPOCH + timedelta(seconds=260), **_KW)
    assert watch.low_since is None
    # A second low streak starting now must get its own full 300s, not
    # continue counting from the first streak's start.
    restart_at = _EPOCH + timedelta(seconds=260)
    watch.advance(colours=2, now=restart_at, **_KW)
    still_within = restart_at + timedelta(seconds=290)
    assert watch.advance(colours=2, now=still_within, **_KW) == Action.NOTHING


def test_stuck_again_within_the_cooldown_window_gives_up_once():
    watch = InstanceWatch()
    watch.advance(colours=2, now=_EPOCH, **_KW)
    first_trigger = _EPOCH + timedelta(seconds=301)
    assert watch.advance(colours=2, now=first_trigger, **_KW) == Action.RESTART

    # Stuck again, still within the cooldown window: give up exactly once.
    stuck_again = first_trigger + timedelta(seconds=301)
    assert watch.advance(colours=2, now=stuck_again, **_KW) == Action.GIVE_UP
    # Repeated checks while still stuck must not spam another give-up.
    assert watch.advance(colours=2, now=stuck_again + timedelta(seconds=10), **_KW) == Action.NOTHING


def test_a_fresh_restart_is_allowed_once_the_cooldown_elapses():
    watch = InstanceWatch()
    watch.advance(colours=2, now=_EPOCH, **_KW)
    first_trigger = _EPOCH + timedelta(seconds=301)
    assert watch.advance(colours=2, now=first_trigger, **_KW) == Action.RESTART

    # Recovers, then gets stuck again well after the cooldown has elapsed.
    recovered = first_trigger + timedelta(seconds=60)
    watch.advance(colours=800, now=recovered, **_KW)
    stuck_later = first_trigger + timedelta(seconds=3700)
    watch.advance(colours=2, now=stuck_later, **_KW)
    triggers_again = stuck_later + timedelta(seconds=301)
    assert watch.advance(colours=2, now=triggers_again, **_KW) == Action.RESTART


# --------------------------------------------------------------------------- #
# WatchdogRegistry — per-instance state, process-lifetime only (by design)
# --------------------------------------------------------------------------- #
def test_registry_returns_the_same_watch_for_repeated_gets():
    registry = WatchdogRegistry()
    a = registry.get("instance-1")
    a.low_since = _EPOCH
    b = registry.get("instance-1")
    assert b is a
    assert b.low_since == _EPOCH


def test_registry_discard_drops_state_for_a_no_longer_tracked_instance():
    registry = WatchdogRegistry()
    registry.get("instance-1").low_since = _EPOCH
    registry.discard("instance-1")
    assert "instance-1" not in registry.known_ids()
    # A subsequent get starts clean rather than resurrecting the old streak.
    assert registry.get("instance-1").low_since is None


def test_registry_known_ids_reflects_only_tracked_instances():
    registry = WatchdogRegistry()
    registry.get("a")
    registry.get("b")
    assert registry.known_ids() == {"a", "b"}
