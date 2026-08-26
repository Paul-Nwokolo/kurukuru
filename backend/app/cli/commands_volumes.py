"""
``kurukuru volumes ls|create|attach|detach|rm`` — additional disks.

Attach and detach need a stopped instance. That is not tidiness: this
hypervisor's hot-unplug removes a disk without waiting for the guest, which
corrupts a mounted filesystem. The API's 409 explains it; this module does not
paraphrase.

A new volume is **unformatted**. Nothing here formats or mounts it — the
guest's filesystem is the guest's decision — so `create` and `attach` print
the three commands that finish the job.
"""

from __future__ import annotations

import time
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli.client import ApiClient
from app.cli.errors import CliError, ExitCode
from app.cli.formats import relative_age
from app.cli.naming import CLI_NAME
from app.cli.output import Output
from app.cli.support import DEFAULT_WAIT_TIMEOUT, POLL_SECONDS, client_of, resolve_instance

volumes_app = typer.Typer(
    help="Additional disks that outlive the instances they attach to.",
    no_args_is_help=True,
)

# `kurukuru volumes snapshot ...` lives under volumes rather than beside
# `kurukuru snapshot`, which already means an instance's disk. Mounted here, and
# imported at the bottom of this module, because that sub-app resolves volumes
# through resolve_volume above.


_STATUS_STYLES = {
    "Available": "green",
    "Attached": "cyan",
    "Creating": "yellow",
    "Error": "red",
}

#: Printed after an attach. The guest sees a raw, unpartitioned device; these
#: are the commands that turn it into something with files in it.
_GUEST_STEPS = (
    "In the guest, once it is running:\n"
    "  lsblk                        # confirm the device name\n"
    "  sudo mkfs.ext4 /dev/vdb      # ONCE per volume — this erases it\n"
    "  sudo mkdir -p /mnt/data && sudo mount /dev/vdb /mnt/data\n"
    "\n"
    "For /etc/fstab, mount by UUID rather than device path — `blkid /dev/vdb`\n"
    "gives it. The path is a position; the UUID is the disk."
)

#: The same, for a guest with no /dev to speak of.
_WINDOWS_GUEST_STEPS = (
    "In the guest, once it is running, open Disk Management (diskmgmt.msc):\n"
    "  bring the new disk Online, Initialise it (GPT), then create and\n"
    "  format a volume on it — ONCE per volume; this erases it.\n"
    "\n"
    "Or from PowerShell: Get-Disk, Initialize-Disk, New-Partition,\n"
    "Format-Volume. Assign a drive letter or mount point rather than relying\n"
    "on the disk number — the number is a position, the volume GUID is the\n"
    "disk."
)


def guest_device(volume: dict) -> str:
    """What the *guest* will call this disk, in that guest's own vocabulary.

    ``device_hint`` is a Linux virtio name and was printed unconditionally, so
    a volume on a Windows instance advertised a ``/dev`` path that guest does
    not have. The API carries both spellings and which guest is attached; this
    picks the one the reader can actually act on.
    """
    if volume.get("attached_instance_guest_os") == "windows":
        return volume.get("windows_disk_hint") or ""
    return volume.get("device_hint") or ""


def resolve_volume(client: ApiClient, reference: str) -> dict:
    """Find one volume by id or by name. Ids win, as everywhere else."""
    volumes = client.volumes()
    for volume in volumes:
        if volume["id"] == reference:
            return volume
    matches = [v for v in volumes if v["name"] == reference]
    if len(matches) > 1:
        raise CliError(
            f"'{reference}' matches {len(matches)} volumes in different projects.",
            ExitCode.CONFLICT,
            hint="Pass the id, or scope the command with --project.",
        )
    if matches:
        return matches[0]
    raise CliError(
        f"No volume named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} volumes ls' to see them.",
    )


@volumes_app.command("ls")
def ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List volumes."""
    from app.cli.commands_projects import scoped_project_id

    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volumes = client.volumes(project_id=scoped_project_id(ctx, client))

    if json_out:
        out.emit(volumes)
        return
    if not volumes:
        out.human("[dim]no volumes[/dim]")
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("SIZE", justify="right")
    table.add_column("STATUS")
    table.add_column("ATTACHED TO")
    table.add_column("DEVICE")
    table.add_column("AGE", justify="right")
    for volume in volumes:
        status = volume["status"]
        table.add_row(
            escape(volume["name"]),
            f"{volume['size_gb']}G",
            f"[{_STATUS_STYLES.get(status, 'white')}]{status}[/]",
            escape(volume.get("attached_instance_name") or ""),
            guest_device(volume),
            relative_age(volume.get("created_at")),
        )
    out.human(table)
    for volume in volumes:
        if volume.get("error_message"):
            out.warn(f"{volume['name']}: {volume['error_message']}")


@volumes_app.command("create")
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name for the volume.")],
    size: Annotated[str, typer.Argument(help="Size, e.g. 10G or 10.")],
    wait: Annotated[bool, typer.Option("--wait", help="Poll until it is Available.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Create a blank volume. It is unformatted until the guest formats it."""
    from app.cli.commands_projects import scoped_project_id
    from app.cli.formats import parse_disk_gb

    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = client.create_volume(
        name, parse_disk_gb(size), project_id=scoped_project_id(ctx, client)
    )

    if wait:
        volume = _wait_for_volume(client, out, volume["id"], timeout=timeout)

    if json_out:
        out.emit(volume)
    else:
        out.human(f"{volume['name']}  {volume['id']}  {volume['status']}")
        out.note(f"Attach it with '{CLI_NAME} volumes attach {volume['name']} INSTANCE'.")


def _wait_for_volume(client: ApiClient, out: Output, volume_id: str, *, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    with out.spinner("Allocating…") as progress:
        while True:
            current = client.volume(volume_id)
            progress.update(f"Allocating: {current['status']}")
            if current["status"] == "Error":
                raise CliError(
                    current.get("error_message") or "The volume could not be created.",
                    ExitCode.FAILURE,
                )
            if current["status"] in ("Available", "Attached"):
                return current
            if time.monotonic() >= deadline:
                raise CliError(
                    f"Timed out after {timeout}s (last seen: {current['status']}).",
                    ExitCode.TIMEOUT,
                    hint="The allocation continues on the backend.",
                )
            time.sleep(POLL_SECONDS)


@volumes_app.command("attach")
def attach(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    instance_ref: Annotated[str, typer.Argument(metavar="INSTANCE")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Attach a volume to a stopped instance. Takes effect on its next start."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = resolve_volume(client, reference)
    instance = resolve_instance(client, instance_ref)

    attached = client.attach_volume(volume["id"], instance["id"])

    if json_out:
        out.emit(attached)
    else:
        windows = attached.get("attached_instance_guest_os") == "windows"
        device = guest_device(attached) or "a new device"
        out.human(f"{volume['name']} attached to {instance['name']} as {device}")
        out.note(f"Start it with '{CLI_NAME} start {instance['name']}'.")
        out.note(
            _WINDOWS_GUEST_STEPS
            if windows
            else _GUEST_STEPS.replace("/dev/vdb", f"/dev/{device}")
        )


@volumes_app.command("detach")
def detach(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Detach a volume. Its data is untouched and it becomes Available."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    volume = resolve_volume(client, reference)

    detached = client.detach_volume(volume["id"])

    if json_out:
        out.emit(detached)
    else:
        out.human(f"{volume['name']} detached and Available")


@volumes_app.command("rm")
def rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="VOLUME")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Delete a volume **and its data**. Refused while attached."""
    out = Output()
    client = client_of(ctx)
    volume = resolve_volume(client, reference)

    out.confirm(
        f"This permanently deletes [bold]{escape(volume['name'])}[/bold] "
        f"({volume['size_gb']} GB) [red]and everything on it[/red]. "
        f"There is no snapshot of a volume and no undo.",
        assume_yes=yes,
        action="delete",
    )

    client.delete_volume(volume["id"])
    out.human(f"{volume['name']} deleted")


from app.cli.commands_volume_snapshots import volume_snapshots_app  # noqa: E402

volumes_app.add_typer(volume_snapshots_app, name="snapshot")
