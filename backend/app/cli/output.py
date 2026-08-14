"""
Output discipline: what goes to stdout, what goes to stderr, and when colour.

The scripting contract is the reason this module exists rather than a scatter of
``print`` calls:

* ``--json`` puts **only** JSON on stdout. Nothing else — no headers, no
  spinner, no "waiting…", no warning — may touch that stream, or the first
  ``| jq`` in a pipeline fails on text it can't parse.
* Progress, spinners and warnings go to **stderr** in every mode, so a human
  watching a pipe still sees what is happening while the data stays clean.
* Colour and spinners disable themselves when the stream is not a terminal, or
  when ``NO_COLOR`` is set.

Prompting has the same shape: a script whose stdout is a pipe must never be
asked a question it cannot answer, because the answer is an indefinite hang.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.markup import escape

from app.cli.config import color_disabled
from app.cli.errors import CliError, ExitCode

#: Width used when stdout is not a terminal. Rich would otherwise assume 80
#: columns and truncate instance names and paths in redirected output, which is
#: exactly where truncation is least recoverable.
_PIPED_WIDTH = 200


def _console(*, stderr: bool) -> Console:
    stream = sys.stderr if stderr else sys.stdout
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    return Console(
        stderr=stderr,
        no_color=color_disabled(),
        color_system=None if color_disabled() else "auto",
        width=None if is_tty else _PIPED_WIDTH,
        soft_wrap=not is_tty,
        highlight=False,
    )


class Output:
    """Everything a command is allowed to write, aware of ``--json``."""

    def __init__(self, *, json_mode: bool = False) -> None:
        self.json_mode = json_mode
        self.out = _console(stderr=False)
        self.err = _console(stderr=True)

    # ------------------------------------------------------------------ #
    # Streams
    # ------------------------------------------------------------------ #
    @property
    def stdout_is_tty(self) -> bool:
        return self.out.is_terminal

    @property
    def stderr_is_tty(self) -> bool:
        return self.err.is_terminal

    def emit(self, data: Any) -> None:
        """Write the JSON document — the only thing ``--json`` puts on stdout.

        Redirected output is written raw rather than through rich: a console
        renderer is free to wrap a long line, and a line break inside a JSON
        string is the difference between valid output and a parse error at the
        far end of a pipe. Interactive output goes through rich for the
        syntax colouring, where wrapping is only ever a display artifact.
        """
        # ``default=str`` so a datetime that slipped through serialises instead
        # of failing the command after the work is already done.
        text = json.dumps(data, indent=2, default=str)
        if self.stdout_is_tty and not color_disabled():
            self.out.print_json(text, indent=2)
        else:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()

    def human(self, *args: Any, **kwargs: Any) -> None:
        """Human-facing stdout. Silent in ``--json`` mode, by design."""
        if self.json_mode:
            return
        self.out.print(*args, **kwargs)

    def note(self, message: str) -> None:
        """Progress and context. Always stderr, so pipes stay clean."""
        self.err.print(f"[dim]{escape(message)}[/dim]")

    def warn(self, message: str) -> None:
        self.err.print(f"[yellow]warning:[/yellow] {escape(message)}")

    def error(self, message: str, hint: str | None = None) -> None:
        self.err.print(f"[bold red]error:[/bold red] {escape(message)}")
        if hint:
            self.err.print(f"[dim]hint: {escape(hint)}[/dim]")

    # ------------------------------------------------------------------ #
    # Progress
    # ------------------------------------------------------------------ #
    @contextmanager
    def spinner(self, message: str) -> Iterator["Progress"]:
        """A live status on stderr, degrading to plain lines when not a TTY.

        A spinner redrawn into a log file produces thousands of control
        sequences and no information, so non-interactive callers get one line
        per *change* of state instead.
        """
        if self.stderr_is_tty:
            with self.err.status(message, spinner="dots") as status:
                yield _LiveProgress(status)
        else:
            self.note(message)
            yield _QuietProgress(self)

    # ------------------------------------------------------------------ #
    # Prompting
    # ------------------------------------------------------------------ #
    def confirm(self, question: str, *, assume_yes: bool, action: str) -> None:
        """Ask before something destructive — or refuse to ask, and say so.

        Non-interactive callers are not prompted at all. A script whose stdout
        is a pipe cannot answer, and a CLI that asks anyway hangs a CI job until
        it is killed. ``--yes`` is how a script says it meant it.
        """
        if assume_yes:
            return
        if not self.stdout_is_tty:
            raise CliError(
                "Refusing to prompt for confirmation: stdout is not a terminal.",
                ExitCode.USAGE,
                hint=f"Pass --yes to {action} without asking.",
            )
        self.out.print(question)
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except EOFError:  # a terminal that closed stdin mid-prompt
            answer = ""
        if answer not in ("y", "yes"):
            raise CliError("Cancelled.", ExitCode.FAILURE)


class Progress:
    """Something a long operation can push its current state into."""

    def update(self, message: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class _LiveProgress(Progress):
    def __init__(self, status: Any) -> None:
        self._status = status

    def update(self, message: str) -> None:
        self._status.update(message)


class _QuietProgress(Progress):
    """Prints only when the message changes, so logs stay readable."""

    def __init__(self, output: Output) -> None:
        self._output = output
        self._last: str | None = None

    def update(self, message: str) -> None:
        if message != self._last:
            self._output.note(message)
            self._last = message
