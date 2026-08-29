"""
Failure as a value: one exception carrying the exit code it should produce.

The exit-code table is a contract with scripts, so it lives in one enum and
every failure path picks a member of it. A command that discovers a problem
raises :class:`CliError`; the top-level wrapper in ``main`` renders it and exits.
No command calls ``sys.exit`` with a bare integer.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """The documented exit codes. Scripts branch on these."""

    OK = 0
    #: Something went wrong that has no more specific code — including a
    #: launch that reached the Error state, and any 5xx from the API.
    FAILURE = 1
    #: Bad usage: unknown flag, missing argument, contradictory options. Click
    #: already exits 2 for the ones it parses; we match it for the ones it
    #: can't (e.g. a confirmation that cannot be asked for).
    USAGE = 2
    #: The API could not be reached at all. Distinct from every other failure
    #: because the remedy is different: start the backend, or point at it.
    UNREACHABLE = 3
    NOT_FOUND = 4       # API 404, or a name that matches nothing
    CONFLICT = 5        # API 409 — wrong state for the operation
    INVALID = 6         # API 422 — validation or a capacity refusal
    TIMEOUT = 7         # --wait gave up before the instance settled
    #: API 401. Its own code because the remedy is specific and nothing
    #: else shares it: sign in, or create the first account.
    UNAUTHENTICATED = 8


class CliError(Exception):
    """An error with a user-facing message and the exit code it maps to.

    ``hint`` is the next thing to try. It is printed under the message and is
    the difference between a CLI that reports failure and one that helps —
    every raise site that can name an action should carry one.
    """

    def __init__(
        self,
        message: str,
        code: ExitCode = ExitCode.FAILURE,
        *,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
