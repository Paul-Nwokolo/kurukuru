"""
``iaas events`` — read the event log.

A single command rather than a sub-app: there is nothing to do to an event but
read it. With an instance named it shows that instance's history; without one,
everything, which is the shape you want when something went wrong and you do
not yet know what to blame.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli.errors import CliError, ExitCode
from app.cli.formats import absolute_time, relative_age
from app.cli.output import Output
from app.cli.support import client_of, resolve_instance

#: Glyph per kind. ASCII on purpose — this goes to terminals whose font and
#: code page are not ours to assume, and a box-drawing placeholder in place of
#: an emoji is worse than a symbol that was never fancy.
_MARKS = {
    "created": ("+", "cyan"),
    "provisioning_started": ("~", "yellow"),
    "provisioning_succeeded": ("*", "green"),
    "provisioning_failed": ("x", "red"),
    "started": (">", "green"),
    "stopped": ("#", "blue"),
    "terminated": ("-", "dim"),
    "force_terminated": ("-", "red"),
    "errored": ("!", "red"),
    "snapshot_created": ("[]", "cyan"),
    "snapshot_restored": ("<", "magenta"),
    "snapshot_deleted": ("][", "dim"),
    "restarted": ("^", "green"),
    "cloned": ("=+", "cyan"),
    "volume_attached": ("+]", "cyan"),
    "volume_detached": ("[-", "dim"),
    "port_forward_added": ("->", "cyan"),
    "port_forward_removed": ("-x", "dim"),
    "reconciled": ("=", "yellow"),
    "image_import": ("^", "cyan"),
}

#: Kinds accepted for --kind, kept in sync with the API's enum by the test that
#: reads the OpenAPI schema. A bad value is a usage error here rather than a
#: 422 from the server, so the message can list the alternatives.
KNOWN_KINDS = tuple(_MARKS)


def register(app: typer.Typer) -> None:
    app.command("events")(events)


def events(
    ctx: typer.Context,
    reference: Annotated[
        str | None,
        typer.Argument(
            metavar="[NAME_OR_ID]",
            help="Show one instance's history. Omit for everything.",
        ),
    ] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help=f"One of: {', '.join(KNOWN_KINDS)}.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many to show.")] = 50,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Show what has happened, newest first.

    The log records what the instance rows cannot: every stop and start rather
    than the most recent one, every error rather than the last, corrections the
    reconciler made on its own, and restores — which rewrite a disk and change
    no record anywhere else.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)

    if kind is not None and kind not in KNOWN_KINDS:
        raise CliError(
            f"Unknown event kind '{kind}'.",
            ExitCode.USAGE,
            hint=f"Known kinds: {', '.join(KNOWN_KINDS)}.",
        )

    instance_id = None
    if reference is not None:
        # Terminated instances are exactly the ones whose history is worth
        # reading, so they must resolve here.
        instance_id = resolve_instance(client, reference, include_terminated=True)["id"]

    entries = client.events(instance_id=instance_id, kind=kind, limit=limit)

    if json_out:
        out.emit(entries)
        return
    if not entries:
        out.human("[dim]no events[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("")
    table.add_column("AGE", justify="right")
    if instance_id is None:
        table.add_column("INSTANCE")
    table.add_column("EVENT")
    table.add_column("ACTOR")

    for entry in entries:
        mark, style = _MARKS.get(entry["kind"], ("?", "white"))
        row = [f"[{style}]{mark}[/]", relative_age(entry.get("occurred_at"))]
        if instance_id is None:
            row.append(escape(entry.get("instance_name") or "—"))
        row.extend([escape(entry.get("summary") or entry["kind"]), entry.get("actor", "")])
        table.add_row(*row)

    out.human(table)

    # Details are where the reason lives — a failure's stderr, or what the
    # reconciler saw. Printing them inline would bury the timeline, so only the
    # newest entry's is offered, and only when it has one.
    newest = entries[0]
    if newest.get("detail"):
        out.human("")
        out.human(f"[dim]{absolute_time(newest.get('occurred_at'))}[/dim]")
        for line in str(newest["detail"]).splitlines():
            out.human(f"  {escape(line)}")
    if len(entries) == limit:
        out.note(f"Showing the newest {limit}; raise it with --limit.")
