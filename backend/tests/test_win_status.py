"""Turning a Windows status code into something a user can act on.

The bug this file exists for produced one line of output:

    qemu-system-x86_64.exe --version failed (exit 3236495362)

That is a signed 32-bit NTSTATUS printed in decimal, so it does not look like
a status code — it looks like a program that exited with a big number. The
user took it to an AI assistant, which converted it wrongly and told them to
add QEMU to PATH: a fix for a problem they did not have, on an install whose
path resolution was already correct.

In hex it is 0xC0E90002, and Windows' own message for it is "An Application
Control policy has blocked this file."
"""

from __future__ import annotations

import sys

import pytest

from kurukuru.win_status import describe_exit_code, is_status_code


#: The exact number from the report.
_SMART_APP_CONTROL = 3236495362


def test_the_reported_code_is_explained_in_hex_with_a_name():
    """The three things the decimal number did not carry."""
    described = describe_exit_code(_SMART_APP_CONTROL)

    # Hex, because that is the spelling every reference and every search uses.
    assert "0xC0E90002" in described
    # A name, so it can be looked up.
    assert "STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION" in described
    # And a sentence, because the name alone still needs a translator.
    assert "Smart App Control" in described
    assert "not signed" in described


def test_the_explanation_does_not_send_anyone_to_PATH():
    """The specific wrong turn this is meant to prevent."""
    described = describe_exit_code(_SMART_APP_CONTROL).lower()
    assert "path" not in described


def test_an_ordinary_exit_status_gets_no_hex():
    """'exit 1' in hex helps nobody, and noise is what makes signal unreadable."""
    assert describe_exit_code(1) == ""
    assert describe_exit_code(0) == ""
    assert describe_exit_code(2) == ""


def test_the_signed_and_unsigned_spellings_are_the_same_status():
    """Python reports these differently depending on where they come from."""
    assert describe_exit_code(-1058471934) == describe_exit_code(_SMART_APP_CONTROL)


@pytest.mark.parametrize(
    "code",
    [0xC0000022, 0xC0000135, 0xC0000142, 0xC0000005, 0xC0000409],
)
def test_every_tabled_status_carries_hex_a_name_and_advice(code: int):
    described = describe_exit_code(code)
    assert f"0x{code:08X}" in described
    assert "STATUS_" in described
    # Two sentences at least: the name, then what to do about it.
    assert described.count(".") >= 2


def test_an_untabled_status_still_says_it_is_one():
    """The fallback must not leave a bare number looking like an exit code."""
    described = describe_exit_code(0xC0000225)
    assert "0xC0000225" in described
    assert "Windows status code" in described


@pytest.mark.skipif(sys.platform != "win32", reason="reads ntdll's message table")
def test_windows_own_message_is_used_for_codes_we_have_not_tabled():
    """The generic path, which is what makes a *new* failure diagnosable.

    This is also a regression test for a real defect in the first version of
    this module: ctypes was left to guess the signatures, so the 64-bit module
    handle from GetModuleHandleW was truncated to an int and FormatMessageW
    silently found nothing for every code. It returned no message and no
    error, which is the worst way for a lookup to fail.
    """
    described = describe_exit_code(0xC000007B)
    assert "0xC000007B" in described
    assert "Bad Image" in described, (
        "ntdll's own message text was not used — check the ctypes argtypes"
    )


def test_is_status_code_only_claims_the_high_range():
    assert is_status_code(_SMART_APP_CONTROL)
    assert is_status_code(0xC0000005)
    assert not is_status_code(0)
    assert not is_status_code(1)
    assert not is_status_code(255)


def test_describing_a_code_never_raises():
    """This runs inside error paths. A formatter that can fail is a trap."""
    for value in (0, 1, -1, 2**31, -(2**31), 0xFFFFFFFF, 12345678):
        assert isinstance(describe_exit_code(value), str)
