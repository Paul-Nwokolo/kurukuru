"""
Snapshot commands: ``kurukuru snapshot create|ls|restore|rm``.

Same conventions as everything else: ``--json`` on the read, ``--wait`` on the
asynchronous operations, exit codes from the table, and no prompt when stdout is
not a terminal.

Restore gets the confirmation treatment that ``rm`` gets, because it is equally
destructive and less obviously so — "restore" sounds like recovery, and what it
actually does is discard everything written since the snapshot.
"""

from __future__ import annotations

import time
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from kurukuru.cli.client import ApiClient
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import absolute_time, format_bytes, relative_age
from kurukuru.cli.naming import CLI_NAME
from kurukuru.cli.output import Output
from kurukuru.cli.support import (
    DEFAULT_WAIT_TIMEOUT,
    POLL_SECONDS,
    client_of,
    resolve_instance,
)

snapshots_app = typer.Typer(
    help="Point-in-time snapshots of an instance's disk.", no_args_is_help=True
)

_SETTLED = {"Available", "Error"}

_STATUS_STYLES = {
    "Available": "green",
    "Creating": "yellow",
    "Deleting": "yellow",
    "Error": "red",
}


def _resolve_snapshot(client: ApiClient, instance: dict, reference: str) -> dict:
    """Find a snapshot by name or id within one instance."""
    snapshots = client.snapshots(instance["id"])
    for snapshot in snapshots:
        if reference in (snapshot["id"], snapshot["name"]):
            return snapshot
    raise CliError(
        f"'{instance['name']}' has no snapshot named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} snapshot ls {instance['name']}' to see them.",
    )


def _wait_for_snapshot(
    client: ApiClient, out: Output, instance_id: str, snapshot_id: str,
    *, timeout: int, label: str,
) -> dict:
    """Poll until the snapshot settles, or fail with the reason it didn't."""
    deadline = time.monotonic() + timeout
    with out.spinner(f"{label}…") as progress:
        while True:
            current = next(
                (s for s in client.snapshots(instance_id) if s["id"] == snapshot_id), None
            )
            if current is None:
                # Deleting removes the row; that *is* the success signal.
                return {"id": snapshot_id, "status": "Deleted"}
            progress.update(f"{label}: {current['status']}")
            if current["status"] == "Error":
                raise CliError(
                    current.get("error_message") or "The snapshot failed.",
                    ExitCode.FAILURE,
                )
            if current["status"] in _SETTLED:
                return current
            if time.monotonic() >= deadline:
                raise CliError(
                    f"Timed out after {timeout}s waiting for the snapshot "
                    f"(last seen: {current['status']}).",
                    ExitCode.TIMEOUT,
                    hint="The operation continues on the backend.",
                )
            time.sleep(POLL_SECONDS)


@snapshots_app.command("create")
def create(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    name: Annotated[str, typer.Argument(help="Name for the snapshot.")],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="What this captures.")
    ] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Poll until it is Available.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Snapshot an instance's disk.

    The instance must be stopped: this hypervisor cannot save a running guest's
    memory, and capturing its disk underneath it would preserve a filesystem
    mid-write. The API's 409 explains it if you forget.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)

    snapshot = client.create_snapshot(instance["id"], name, description)

    if wait:
        snapshot = _wait_for_snapshot(
            client, out, instance["id"], snapshot["id"],
            timeout=timeout, label=f"Snapshotting {instance['name']}",
        )

    if json_out:
        out.emit(snapshot)
    else:
        out.human(f"{snapshot['name']}  {snapshot['id']}  {snapshot['status']}")
        if not wait:
            out.note(
                f"Running in the background. Follow it with "
                f"'{CLI_NAME} snapshot ls {instance['name']}'."
            )


@snapshots_app.command("ls")
def ls(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List an instance's snapshots."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference, include_terminated=True)
    snapshots = client.snapshots(instance["id"])

    if json_out:
        out.emit(snapshots)
        return
    if not snapshots:
        out.human("[dim]no snapshots[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("STATUS")
    table.add_column("SIZE", justify="right")
    table.add_column("AGE", justify="right")
    table.add_column("DESCRIPTION")
    for snapshot in snapshots:
        status = snapshot["status"]
        table.add_row(
            escape(snapshot["name"]),
            f"[{_STATUS_STYLES.get(status, 'white')}]{status}[/]",
            format_bytes(snapshot.get("size_bytes")),
            relative_age(snapshot.get("created_at")),
            escape(snapshot.get("description") or ""),
        )
    out.human(table)
    for snapshot in snapshots:
        if snapshot.get("error_message"):
            out.warn(f"{snapshot['name']}: {snapshot['error_message']}")


@snapshots_app.command("restore")
def restore(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    snapshot_ref: Annotated[str, typer.Argument(metavar="SNAPSHOT")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Roll an instance back to a snapshot.

    Everything written since is discarded. Confirmed by default for that
    reason — "restore" reads like recovery, but for anything created after the
    snapshot it is a deletion.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)
    snapshot = _resolve_snapshot(client, instance, snapshot_ref)

    out.confirm(
        f"This returns [bold]{instance['name']}[/bold] to "
        f"'{escape(snapshot['name'])}' ({relative_age(snapshot.get('created_at'))} old) "
        f"and discards every change made since.",
        assume_yes=yes,
        action="restore",
    )

    with out.spinner(f"Restoring {instance['name']}…"):
        restored = client.restore_snapshot(instance["id"], snapshot["id"])

    if json_out:
        out.emit(restored)
    else:
        out.human(f"{instance['name']} restored to {snapshot['name']}")
        out.note(f"Start it with '{CLI_NAME} start {instance['name']}'.")


@snapshots_app.command("rm")
def rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    snapshot_ref: Annotated[str, typer.Argument(metavar="SNAPSHOT")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Poll until it is gone.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Delete a snapshot. The instance itself is untouched."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)
    snapshot = _resolve_snapshot(client, instance, snapshot_ref)

    out.confirm(
        f"This permanently deletes the snapshot "
        f"[bold]{escape(snapshot['name'])}[/bold] of {instance['name']}.",
        assume_yes=yes,
        action="delete",
    )

    deleted = client.delete_snapshot(instance["id"], snapshot["id"])

    if wait:
        deleted = _wait_for_snapshot(
            client, out, instance["id"], snapshot["id"],
            timeout=timeout, label=f"Deleting {snapshot['name']}",
        )

    if json_out:
        out.emit(deleted)
    else:
        out.human(f"{snapshot['name']} {'deleted' if wait else 'is Deleting'}")


def snapshot_detail(out: Output, snapshot: dict) -> None:
    """Shared renderer for a single snapshot, used by `show`-style output."""
    out.human(f"  name        {snapshot['name']}")
    out.human(f"  status      {snapshot['status']}")
    out.human(f"  created     {absolute_time(snapshot.get('created_at'))}")
