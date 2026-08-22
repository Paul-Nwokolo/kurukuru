"""
``iaas volumes snapshot create|ls|restore|rm`` — point-in-time volume state.

A sub-app under ``volumes`` rather than a second top-level ``snapshot`` command,
because the resource being snapshotted is what the user is already thinking
about, and ``iaas snapshot`` already means "an instance's disk".

**These are two independent things and the copy says so every time.** An
instance snapshot does not include attached volumes; a volume snapshot does not
include the instance. Restoring one does not restore the other. That sentence is
repeated in the confirmations rather than stated once in a help string, because
the moment it matters is the moment somebody is about to discard data.

Restore is confirmed by default for the same reason it is on instances:
"restore" reads like recovery, but for anything written since the snapshot it is
a deletion.
"""

from __future__ import annotations

import time
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli.client import ApiClient
from app.cli.errors import CliError, ExitCode
from app.cli.formats import format_bytes, relative_age
from app.cli.naming import CLI_NAME
from app.cli.output import Output
from app.cli.support import DEFAULT_WAIT_TIMEOUT, POLL_SECONDS, client_of

volume_snapshots_app = typer.Typer(
    help="Point-in-time snapshots of a volume's disk.", no_args_is_help=True
)

_SETTLED = {"Available", "Error"}

_STATUS_STYLES = {
    "Available": "green",
    "Creating": "yellow",
    "Deleting": "yellow",
    "Error": "red",
}

#: Said in full at every decision point. See the module docstring.
_NOT_THE_INSTANCE = (
    "A volume snapshot covers this volume only — not any instance it is "
    "attached to."
)


def _resolve_volume(client: ApiClient, reference: str) -> dict:
    # Imported here rather than at module scope: commands_volumes is what
    # mounts this sub-app, so a top-level import would be circular.
    from app.cli.commands_volumes import resolve_volume

    return resolve_volume(client, reference)


def _resolve_snapshot(client: ApiClient, volume: dict, reference: str) -> dict:
    """Find a snapshot by name or id within one volume."""
    for snapshot in client.volume_snapshots(volume["id"]):
        if reference in (snapshot["id"], snapshot["name"]):
            return snapshot
    raise CliError(
        f"'{volume['name']}' has no snapshot named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} volumes snapshot ls {volume['name']}' to see them.",
    )


def _wait_for_snapshot(
    client: ApiClient,
    out: Output,
    volume_id: str,
    snapshot_id: str,
    *,
    timeout: int,
    label: str,
) -> dict:
    """Poll until the snapshot settles or disappears. Mirrors the instance one."""
    deadline = time.time() + timeout
    last: dict = {"id": snapshot_id, "status": "Creating"}
    with out.spinner(f"{label}…"):
        while time.time() < deadline:
            rows = {s["id"]: s for s in client.volume_snapshots(volume_id)}
            current = rows.get(snapshot_id)
            if current is None:
                # Deleted: the row is gone, which is the success case for rm.
                return last
            last = current
            if current["status"] in _SETTLED:
                break
            time.sleep(POLL_SECONDS)

    if last.get("status") == "Error":
        raise CliError(
            last.get("error_message") or "The snapshot failed.", ExitCode.UNAVAILABLE
        )
    return last


@volume_snapshots_app.command("create")
def create(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    name: Annotated[str, typer.Argument(help="Name for the snapshot.")],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="What this captures.")
    ] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Poll until it is Available.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Snapshot a volume's disk.

    The volume may stay attached, but any instance holding it must be stopped:
    copying the file while a guest is writing to it would capture a filesystem
    mid-write. The API's 409 explains it if you forget.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = _resolve_volume(client, reference)

    snapshot = client.create_volume_snapshot(volume["id"], name, description)

    if wait:
        snapshot = _wait_for_snapshot(
            client, out, volume["id"], snapshot["id"],
            timeout=timeout, label=f"Snapshotting {volume['name']}",
        )

    if json_out:
        out.emit(snapshot)
    else:
        out.human(f"{snapshot['name']}  {snapshot['id']}  {snapshot['status']}")
        out.note(_NOT_THE_INSTANCE)
        if not wait:
            out.note(
                f"Running in the background. Follow it with "
                f"'{CLI_NAME} volumes snapshot ls {volume['name']}'."
            )


@volume_snapshots_app.command("ls")
def ls(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List a volume's snapshots."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = _resolve_volume(client, reference)
    snapshots = client.volume_snapshots(volume["id"])

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


@volume_snapshots_app.command("restore")
def restore(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    snapshot_ref: Annotated[str, typer.Argument(metavar="SNAPSHOT")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Roll a volume back to a snapshot.

    Everything written to the volume since is discarded. The instance it is
    attached to is **not** rolled back — if that instance has state that expects
    the newer contents of this disk, restoring here will leave the two
    disagreeing.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = _resolve_volume(client, reference)
    snapshot = _resolve_snapshot(client, volume, snapshot_ref)

    attached = volume.get("attached_instance_name")
    where = f" It is attached to [bold]{attached}[/bold], which is not restored." if attached else ""
    out.confirm(
        f"This returns the volume [bold]{volume['name']}[/bold] to "
        f"'{escape(snapshot['name'])}' ({relative_age(snapshot.get('created_at'))} old) "
        f"and discards every change made to it since.{where}",
        assume_yes=yes,
        action="restore",
    )

    with out.spinner(f"Restoring {volume['name']}…"):
        restored = client.restore_volume_snapshot(volume["id"], snapshot["id"])

    if json_out:
        out.emit(restored)
    else:
        out.human(f"{volume['name']} restored to {snapshot['name']}")
        out.note(_NOT_THE_INSTANCE)


@volume_snapshots_app.command("rm")
def rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    snapshot_ref: Annotated[str, typer.Argument(metavar="SNAPSHOT")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Poll until it is gone.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Delete a volume snapshot. The volume's current contents are untouched."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = _resolve_volume(client, reference)
    snapshot = _resolve_snapshot(client, volume, snapshot_ref)

    out.confirm(
        f"This permanently deletes the snapshot "
        f"[bold]{escape(snapshot['name'])}[/bold] of volume {volume['name']}.",
        assume_yes=yes,
        action="delete",
    )

    deleted = client.delete_volume_snapshot(volume["id"], snapshot["id"])

    if wait:
        deleted = _wait_for_snapshot(
            client, out, volume["id"], snapshot["id"],
            timeout=timeout, label=f"Deleting {snapshot['name']}",
        )

    if json_out:
        out.emit(deleted)
    else:
        out.human(f"{snapshot['name']} {'deleted' if wait else 'is Deleting'}")
