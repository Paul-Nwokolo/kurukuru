"""
Integration tests for ``windows_reboot_watchdog_pass`` — the DB/engine wiring
around the pure logic tested in test_reboot_watchdog.py. Screendumps are
patched out entirely: this exercises scoping (Windows/Running/qemu only),
the restart action, the event it records, and the give-up-within-cooldown
path, not the QMP protocol itself (that's test_qmp.py's job).
"""

from __future__ import annotations

from unittest.mock import patch

from sqlmodel import Session

from kurukuru.config import Settings
from kurukuru.models import EventKind, GuestOS, Instance, InstanceStatus
from kurukuru.routers.instances import _reboot_watchdog, windows_reboot_watchdog_pass
from tests.test_instances_api import client, iso_dir  # noqa: F401 - fixtures

_WATCHDOG_SETTINGS = Settings(
    windows_reboot_watchdog_colour_threshold=8,
    windows_reboot_watchdog_stuck_seconds=300.0,
    windows_reboot_watchdog_cooldown_seconds=3600.0,
)


def _add_instance(session: Session, **overrides) -> Instance:
    defaults = dict(
        name="win-01",
        status=InstanceStatus.RUNNING,
        guest_os=GuestOS.WINDOWS,
        engine="qemu",
        qmp_port=4444,
    )
    instance = Instance(**{**defaults, **overrides})
    session.add(instance)
    session.commit()
    session.refresh(instance)
    return instance


def _events(client, instance_id: str) -> list[dict]:
    return client.get(f"/instances/{instance_id}/events").json()


def setup_function() -> None:
    # The registry is process-lifetime, module-level state by design (see
    # reboot_watchdog.py) — tests must not leak a stuck-since timer from one
    # test into the next.
    for instance_id in _reboot_watchdog.known_ids():
        _reboot_watchdog.discard(instance_id)


def test_a_healthy_windows_guest_is_left_alone(client):
    with Session(client.db_engine) as session:
        instance = _add_instance(session)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.screendump"), patch(
        "kurukuru.routers.instances.colour_count", return_value=800
    ):
        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 0
    assert client.get(f"/instances/{instance_id}").json()["status"] == "Running"
    assert EventKind.AUTO_RESTARTED.value not in {e["kind"] for e in _events(client, instance_id)}


def test_a_linux_guest_is_never_sampled_even_if_it_looks_stuck(client):
    """Scope guard: a static low-colour frame is a normal steady state for a
    headless Linux console, not a symptom. See reboot_watchdog.py."""
    with Session(client.db_engine) as session:
        instance = _add_instance(session, name="linux-01", guest_os=GuestOS.LINUX)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.screendump") as fake_screendump, patch(
        "kurukuru.routers.instances.colour_count", return_value=1
    ):
        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 0
    fake_screendump.assert_not_called()
    assert client.get(f"/instances/{instance_id}").json()["status"] == "Running"


def test_a_stopped_windows_guest_is_never_sampled(client):
    with Session(client.db_engine) as session:
        _add_instance(session, name="win-off", status=InstanceStatus.STOPPED)

    with patch("kurukuru.routers.instances.screendump") as fake_screendump:
        with Session(client.db_engine) as session:
            windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    fake_screendump.assert_not_called()


def test_a_stuck_guest_is_restarted_and_the_event_is_its_own_kind(client):
    with Session(client.db_engine) as session:
        instance = _add_instance(session)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.screendump"), patch(
        "kurukuru.routers.instances.colour_count", return_value=2
    ):
        # First pass starts the clock; nothing has crossed the threshold yet.
        with Session(client.db_engine) as session:
            assert windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS) == 0

        watch = _reboot_watchdog.get(instance_id)
        from datetime import timedelta

        watch.low_since -= timedelta(seconds=301)  # fast-forward past the threshold

        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 1
    assert client.fake.calls[-1] == ("start", "win-01")  # restart_instance = stop then start
    events = _events(client, instance_id)
    kinds = [e["kind"] for e in events]
    assert EventKind.AUTO_RESTARTED.value in kinds
    auto_event = next(e for e in events if e["kind"] == EventKind.AUTO_RESTARTED.value)
    assert auto_event["actor"] == "system"
    assert "restarted automatically" in auto_event["summary"].lower()


def test_stuck_again_within_the_cooldown_marks_error_instead_of_looping(client):
    from datetime import timedelta

    with Session(client.db_engine) as session:
        instance = _add_instance(session)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.screendump"), patch(
        "kurukuru.routers.instances.colour_count", return_value=2
    ):
        with Session(client.db_engine) as session:
            windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)
        watch = _reboot_watchdog.get(instance_id)
        watch.low_since -= timedelta(seconds=301)
        with Session(client.db_engine) as session:
            assert windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS) == 1

        # Re-mark the (fake) instance Running — a real restart would leave it
        # so, but the fake's restart_instance already does; make the stuck
        # state explicit for the next low sample regardless.
        with Session(client.db_engine) as session:
            row = session.get(Instance, instance_id)
            row.status = InstanceStatus.RUNNING
            session.add(row)
            session.commit()

        watch.low_since -= timedelta(seconds=301)  # stuck again, still within cooldown
        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 1
    assert client.get(f"/instances/{instance_id}").json()["status"] == "Error"
    events = _events(client, instance_id)
    assert events[0]["kind"] == EventKind.ERRORED.value
    assert "cooldown" in events[0]["detail"].lower() or "stuck again" in events[0]["detail"].lower()


# --------------------------------------------------------------------------- #
# _restart_with_retry — restart_instance() failing mechanically (e.g. a
# just-force-killed process's own pinned port not yet released) is a
# different axis from the stuck-again/cooldown path above. Giving up after
# exactly one such failure would fail the watchdog at the one moment it
# exists for; see reboot_watchdog.py and _restart_with_retry's docstring.
# --------------------------------------------------------------------------- #
def test_a_transient_restart_failure_is_retried_not_given_up_on(client):
    """The fake's start_instance fails once, as QemuEngine's really did after
    a forced kill — the watchdog must still complete the restart, not mark
    the instance Error on the first hiccup."""
    from datetime import timedelta

    with Session(client.db_engine) as session:
        instance = _add_instance(session)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.time.sleep"), patch(
        "kurukuru.routers.instances.screendump"
    ), patch("kurukuru.routers.instances.colour_count", return_value=2):
        with Session(client.db_engine) as session:
            assert windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS) == 0

        watch = _reboot_watchdog.get(instance_id)
        watch.low_since -= timedelta(seconds=301)

        client.fake.fail_starts = 1  # type: ignore[attr-defined]
        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 1
    assert client.get(f"/instances/{instance_id}").json()["status"] == "Running"
    events = _events(client, instance_id)
    kinds = [e["kind"] for e in events]
    assert EventKind.AUTO_RESTARTED.value in kinds
    assert EventKind.ERRORED.value not in kinds


def test_a_restart_failure_that_never_clears_is_still_given_up_on(client):
    """The other branch of the same guard: a failure that outlasts every
    retry attempt is a real problem, not a transient race, and must still be
    reported rather than retried forever."""
    from datetime import timedelta

    with Session(client.db_engine) as session:
        instance = _add_instance(session)
        instance_id = instance.id

    with patch("kurukuru.routers.instances.time.sleep"), patch(
        "kurukuru.routers.instances.screendump"
    ), patch("kurukuru.routers.instances.colour_count", return_value=2):
        with Session(client.db_engine) as session:
            assert windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS) == 0

        watch = _reboot_watchdog.get(instance_id)
        watch.low_since -= timedelta(seconds=301)

        client.fake.fail_starts = 1_000  # exceeds every retry attempt
        with Session(client.db_engine) as session:
            acted = windows_reboot_watchdog_pass(session, _WATCHDOG_SETTINGS)

    assert acted == 1
    assert client.get(f"/instances/{instance_id}").json()["status"] == "Error"
    events = _events(client, instance_id)
    assert events[0]["kind"] == EventKind.ERRORED.value
    assert "attempt" in events[0]["detail"].lower()
