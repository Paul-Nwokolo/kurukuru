"""Two things ``doctor`` could not see, and now reports.

Both come from one external install that looked healthy from the inside. The
engine reported unavailable with a working QEMU sitting beside the executable,
and nothing in the product could say why — because nothing was looking at
Windows' own application-control state, and nothing noticed that a stale
0.1.0-era override was pointing at a path that did not exist.
"""

from __future__ import annotations

import pytest

from kurukuru.cli.commands_system import (
    FAIL,
    PASS,
    WARN,
    _application_control_checks,
    _override_checks,
)
from kurukuru.win_security import smart_app_control_state


def _only(checks) -> object:
    assert len(checks) == 1, f"expected exactly one check, got {checks}"
    return checks[0]


# --------------------------------------------------------------------------- #
# Smart App Control
# --------------------------------------------------------------------------- #
def test_enforcing_and_a_dead_engine_is_reported_as_the_cause():
    """The reported failure, and the sentence that explains it."""
    check = _only(
        _application_control_checks(
            {
                "smart_app_control": {
                    "state": "enforced",
                    "detail": "Smart App Control is on and enforcing.",
                }
            },
            engine_ok=False,
        )
    )

    assert check.status == FAIL
    assert "Smart App Control" in check.detail
    # Names the cause, says it is not the user's fault, and gives the setting.
    assert "not" in check.remedy and "code-signed" in check.remedy
    assert "Windows Security" in check.remedy


def test_enforcing_while_everything_works_is_only_a_warning():
    """A machine that let this build through is not broken today.

    Worth saying before the next upgrade replaces the files it was allowed on,
    which is a different sentence from "this is why nothing works".
    """
    check = _only(
        _application_control_checks(
            {"smart_app_control": {"state": "enforced", "detail": "on and enforcing"}},
            engine_ok=True,
        )
    )
    assert check.status == WARN
    assert "upgrade" in check.remedy


def test_off_and_working_says_nothing_at_all():
    """doctor's output is read because it is short. Silence is a feature."""
    assert (
        _application_control_checks(
            {"smart_app_control": {"state": "off", "detail": "off"}}, engine_ok=True
        )
        == []
    )


def test_off_but_broken_rules_smart_app_control_out():
    """The other half of a real diagnosis: saying what it is *not*.

    0xC0E90002 is also what an enterprise WDAC policy returns, and a user on a
    managed machine needs to know the fix is not theirs to apply.
    """
    check = _only(
        _application_control_checks(
            {"smart_app_control": {"state": "off", "detail": "Smart App Control is off."}},
            engine_ok=False,
        )
    )
    assert check.status == PASS
    assert "0xC0E90002" in check.remedy
    assert "administrator" in check.remedy


def test_evaluation_mode_is_a_heads_up_not_a_fault():
    check = _only(
        _application_control_checks(
            {"smart_app_control": {"state": "evaluation", "detail": "evaluation mode"}},
            engine_ok=True,
        )
    )
    assert check.status == WARN
    assert "Nothing to do today" in check.remedy


def test_an_unsupported_platform_reports_nothing():
    assert (
        _application_control_checks(
            {"smart_app_control": {"state": "unsupported", "detail": "not Windows"}},
            engine_ok=True,
        )
        == []
    )


def test_reading_the_real_state_never_raises_and_names_itself():
    """Against whatever this machine actually is."""
    state = smart_app_control_state()
    assert state.state in {"off", "enforced", "evaluation", "unsupported", "unknown"}
    assert state.detail
    # blocks_unsigned is the question the rest of the product asks.
    assert state.blocks_unsigned is (state.state == "enforced")


# --------------------------------------------------------------------------- #
# QEMU overrides
# --------------------------------------------------------------------------- #
def test_an_override_pointing_nowhere_fails_and_names_0_1_0():
    """The exact shape of the reported breakage.

    The 0.1.0 release note told people to set these two variables to a
    %LOCALAPPDATA% path. On an all-users install that path does not exist, so
    the workaround replaced a correct resolution with a broken one.
    """
    check = _only(
        _override_checks(
            {
                "qemu_overrides": {
                    "KURUKURU_QEMU_SYSTEM_BINARY": {
                        "in_use": r"C:\Users\x\AppData\Local\Programs\Kurukuru\qemu\qemu-system-x86_64.exe",
                        "would_be": r"C:\Program Files\Kurukuru\qemu\qemu-system-x86_64.exe",
                        "exists": False,
                    }
                }
            }
        )
    )
    assert check.status == FAIL
    assert "no file there" in check.detail
    assert "0.1.0 only" in check.remedy
    # Not `setx VAR ""`: Windows rejects that as invalid syntax and leaves the
    # old value in place, so the instruction looks like it worked and does
    # nothing. This project shipped that advice; the test pins the replacement.
    assert "SetEnvironmentVariable" in check.remedy
    assert 'setx' not in check.remedy


def test_an_override_that_resolves_is_still_worth_saying():
    """It works today and replaces a resolution that would also have worked."""
    check = _only(
        _override_checks(
            {
                "qemu_overrides": {
                    "KURUKURU_QEMU_IMG_BINARY": {
                        "in_use": r"C:\qemu\qemu-img.exe",
                        "would_be": r"C:\Program Files\Kurukuru\qemu\qemu-img.exe",
                        "exists": True,
                    }
                }
            }
        )
    )
    assert check.status == WARN
    assert "0.1.0 only" in check.remedy


def test_no_override_reports_nothing():
    assert _override_checks({}) == []
    assert _override_checks({"qemu_overrides": {}}) == []
