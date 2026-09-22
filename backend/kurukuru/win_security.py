"""What Windows' own application-control features will let this install run.

One feature, for now: **Smart App Control**. It ships on by default on clean
Windows 11 installs, it blocks any program it does not recognise, and when it
blocks one the only thing the victim sees is a process that produced no output
and an exit code in the thirty-two-bit billions.

That is not hypothetical. It is how 0.1.1 failed for a user on a fresh Windows
11 machine: the bundled QEMU was found, resolved and correct, and Windows
refused to load it. Nothing in the product could see why, because nothing in
the product was looking — so the user was left with a working install that
reported "engine unavailable" and a number.

This module is the looking. It reports state as a fact, and
``kurukuru doctor`` turns it into a verdict — the same split every other host
fact in this product uses, for the same reason: what counts as healthy is the
caller's judgement.

**Why the registry rather than an API.** Smart App Control's state lives at
``HKLM\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy`` in
``VerifiedAndReputablePolicyState``, readable by any user without elevation.
There is no supported public API that answers the question, and the settings
UI is not something a backend can consult. The key is documented by Microsoft
for exactly this purpose and is what every other tool reads.

**What it cannot tell you.** ``0xC0E90002`` is also what an enterprise WDAC
policy returns, and this key says nothing about those. So the report below
distinguishes "Smart App Control is enforcing" (a specific, actionable answer)
from "off" — and ``doctor`` is careful to say that a block with SAC off means
some *other* application-control policy, which on a managed machine is the
administrator's to lift.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger("kurukuru.win_security")

#: ``VerifiedAndReputablePolicyState`` values, per Microsoft's documentation.
#:
#: "evaluation" is the interesting middle state: Windows is watching how the
#: machine is used to decide whether to turn enforcement on later. It blocks
#: nothing today and may block everything next week, which is worth saying
#: differently from either of the other two.
_STATES = {
    0: ("off", "Smart App Control is off."),
    1: (
        "enforced",
        "Smart App Control is on and enforcing. It blocks programs it does "
        "not recognise, and Kurukuru's build is not code-signed yet, so it "
        "will block Kurukuru and the QEMU it bundles.",
    ),
    2: (
        "evaluation",
        "Smart App Control is in evaluation mode. It is not blocking "
        "anything yet, but Windows may switch it to enforcing on its own — "
        "at which point it will block Kurukuru and the QEMU it bundles.",
    ),
}

_REGISTRY_PATH = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
_VALUE_NAME = "VerifiedAndReputablePolicyState"


@dataclass(frozen=True)
class SmartAppControl:
    """Smart App Control's state on this host, as a fact.

    ``state`` is one of "off", "enforced", "evaluation", "unsupported" (not
    Windows, or a Windows without the feature) or "unknown" (the key would not
    read, which is itself worth reporting rather than guessing "off" from).
    """

    state: str
    detail: str
    #: The raw registry value, when there was one. Carried so a bug report can
    #: say what was actually read rather than what we made of it.
    raw: int | None = None

    @property
    def blocks_unsigned(self) -> bool:
        """Whether this state will refuse to run an unsigned program today."""
        return self.state == "enforced"

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state, "detail": self.detail, "raw": self.raw}


def smart_app_control_state() -> SmartAppControl:
    """Read Smart App Control's state. Never raises.

    Returns "unsupported" off Windows and on Windows builds predating the
    feature, where the key simply does not exist. That is distinct from
    "unknown", which means the key is there and something went wrong reading
    it — a difference that matters when the question is "is this what blocked
    my install".
    """
    if sys.platform != "win32":
        return SmartAppControl(
            state="unsupported",
            detail="Smart App Control is a Windows feature; this host is not Windows.",
        )

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REGISTRY_PATH) as key:
            value, _kind = winreg.QueryValueEx(key, _VALUE_NAME)
    except FileNotFoundError:
        # Either the key or the value is absent. Both mean the same thing in
        # practice: this Windows does not have the feature, which is every
        # build before Windows 11 22H2.
        return SmartAppControl(
            state="unsupported",
            detail=(
                "This Windows build has no Smart App Control "
                "(it arrived in Windows 11 22H2)."
            ),
        )
    except OSError as exc:
        logger.debug("Could not read Smart App Control state: %s", exc)
        return SmartAppControl(
            state="unknown",
            detail=f"Smart App Control's state could not be read: {exc}",
        )

    try:
        raw = int(value)
    except (TypeError, ValueError):
        return SmartAppControl(
            state="unknown",
            detail=f"Smart App Control's state is an unexpected value: {value!r}",
        )

    state, detail = _STATES.get(
        raw,
        (
            "unknown",
            f"Smart App Control reported state {raw}, which this build does "
            f"not recognise.",
        ),
    )
    return SmartAppControl(state=state, detail=detail, raw=raw)
