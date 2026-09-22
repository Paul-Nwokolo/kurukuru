"""Turning a Windows process exit code into something a person can act on.

This module exists because of one support thread. A user on a fresh Windows 11
machine installed 0.1.1, the engine stayed unavailable, and the log said:

    qemu-system-x86_64.exe --version failed (exit 3236495362)

That number is correct and useless. It is a signed 32-bit NTSTATUS printed in
decimal, so it does not look like a status code at all — it looks like a
process that exited with a large number. The user took it to an AI assistant,
which converted it wrongly and advised adding QEMU to PATH: a fix for a
different problem, on an install where the path resolution was already right.

In hex it is ``0xC0E90002`` — ``STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION`` —
and it means Windows refused to load the image because an application-control
policy said no. Nothing about it is a PATH problem, and the hex spelling is
the difference between a searchable string and a dead end.

Two layers, deliberately:

* **A table of codes we have actually seen**, each with a plain sentence about
  what to do. A name alone is only marginally better than a number:
  "STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION" still needs a translator.
* **Windows' own message text for everything else**, read from ``ntdll.dll``
  through ``FormatMessage``. This is what lets an unrecognised status still
  arrive with a sentence attached rather than a bare number, and it costs one
  call into a DLL that is already loaded in every process.

Never raises, and never returns anything but a plain string: this runs inside
error paths, and an error formatter that can itself fail is a trap.
"""

from __future__ import annotations

import sys

#: Statuses we have diagnosed in the field, and what a user should do about
#: each. The sentence matters more than the name — see the module docstring.
#:
#: Keyed by the unsigned 32-bit value, because that is what the hex spelling
#: everyone searches for actually is.
_KNOWN: dict[int, tuple[str, str]] = {
    0xC0E90002: (
        "STATUS_SYSTEM_INTEGRITY_POLICY_VIOLATION",
        "Windows blocked this program from running because an application "
        "control policy does not trust it. On a home machine that is almost "
        "always Smart App Control, which is on by default on a fresh Windows "
        "11 install and refuses any program that is not signed by a publisher "
        "it recognises. Kurukuru's build is not signed yet, so this is not a "
        "fault on your machine. Run 'kurukuru doctor', which reports Smart App "
        "Control's state and what to do about it.",
    ),
    0xC0000022: (
        "STATUS_ACCESS_DENIED",
        "Windows refused access to the program file. Antivirus quarantine and "
        "a file the current user cannot read are the two usual causes.",
    ),
    0xC0000135: (
        "STATUS_DLL_NOT_FOUND",
        "A DLL the program needs is missing, so it could not start. In a "
        "Kurukuru install this means the bundled QEMU directory is incomplete "
        "— reinstall rather than copying files into it.",
    ),
    0xC0000142: (
        "STATUS_DLL_INIT_FAILED",
        "A DLL the program loaded failed to initialise. This is usually "
        "another program interfering with the process at startup — a security "
        "product injecting itself is the common one.",
    ),
    0xC0000005: (
        "STATUS_ACCESS_VIOLATION",
        "The program crashed. If it is reproducible, it belongs in a bug "
        "report with the command that triggered it.",
    ),
    0xC0000409: (
        "STATUS_STACK_BUFFER_OVERRUN",
        "The program was terminated by Windows' own corruption check. Usually "
        "a damaged or partially-written executable: reinstall.",
    ),
}


def _unsigned(code: int) -> int:
    """The unsigned 32-bit spelling of a code Python reports as signed.

    ``subprocess`` surfaces an NTSTATUS the way the OS handed it over, which
    for anything with the high bit set is a large positive number on Windows
    and can be negative elsewhere. Both spellings name the same status; the
    unsigned one is the one that matches every reference and every search.
    """
    return code & 0xFFFFFFFF


def _system_message(code: int) -> str | None:
    """Windows' own text for a status code, or None if it has none.

    Read from ``ntdll.dll``: exit codes that are NTSTATUS values are not in
    the default system message table, which is why the obvious
    ``FormatMessage`` call returns nothing for them and this one passes
    ``FORMAT_MESSAGE_FROM_HMODULE``.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # argtypes and restype are not decoration here. Without them ctypes
        # defaults every return to a 32-bit int, which truncates the 64-bit
        # module handle GetModuleHandleW returns — and FormatMessageW then
        # silently finds nothing for every code, which is exactly how this
        # was first written and exactly how it failed.
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.FormatMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel32.FormatMessageW.restype = wintypes.DWORD

        ntdll = kernel32.GetModuleHandleW("ntdll.dll")
        if not ntdll:
            return None

        buffer = ctypes.create_unicode_buffer(1024)
        FORMAT_MESSAGE_FROM_HMODULE = 0x00000800
        FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200
        length = kernel32.FormatMessageW(
            FORMAT_MESSAGE_FROM_HMODULE | FORMAT_MESSAGE_IGNORE_INSERTS,
            ntdll,
            _unsigned(code),
            0,
            buffer,
            len(buffer),
            None,
        )
    except Exception:  # noqa: BLE001 - an error formatter must never raise
        return None
    if not length:
        return None

    # System message text is formatted for a dialog box, not a log line: it
    # carries embedded newlines and trailing CRLF. Collapsed to one line so it
    # can be appended to an error without breaking it across the log.
    #
    # Insert placeholders (%1, %hs, %ld) are left as they are. FormatMessage
    # was called with IGNORE_INSERTS because we have no arguments to supply —
    # and a sentence with a literal "%hs" in it still tells the reader far more
    # than the bare number they had before.
    text = " ".join(buffer.value.split()).rstrip(".")
    if len(text) > 300:
        text = text[:297].rstrip() + "..."
    return text or None


def is_status_code(code: int) -> bool:
    """Whether this exit code is an NTSTATUS rather than an ordinary status.

    The high bit set means failure in NTSTATUS terms, and no ordinary program
    exits with a value up there — ``exit(1)`` and friends live at the bottom.
    Used to decide whether hex is worth showing: "exit 1" in hex helps nobody.
    """
    return _unsigned(code) >= 0xC0000000


def describe_exit_code(code: int) -> str:
    """One human sentence about ``code``, or "" when there is nothing to add.

    Returns the empty string for ordinary small exit statuses, so callers can
    append it unconditionally:

        f"failed (exit {code}){describe_exit_code(code)}"

    Deliberately not platform-gated at the top: the table is about Windows
    statuses, and a Linux host that somehow sees one should still get the
    explanation rather than silence.
    """
    if not is_status_code(code):
        return ""

    value = _unsigned(code)
    hexed = f"0x{value:08X}"

    known = _KNOWN.get(value)
    if known is not None:
        name, advice = known
        return f" — {hexed} {name}. {advice}"

    message = _system_message(value)
    if message:
        return f" — {hexed}: {message}. This is a Windows status code, not a program's own exit status."
    return (
        f" — {hexed}. This is a Windows status code, not a program's own exit "
        f"status; searching for that hex value will say what it means."
    )
