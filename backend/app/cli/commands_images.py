"""
Image catalog and boot media: ``images ls|import|rm`` and ``isos ls``.

``import`` is not an upload — the API takes a path on the *backend's*
filesystem and copies it into the image store. On a local-first tool the two are
usually the same machine, but the distinction matters the moment they are not,
so the help text says whose filesystem it means.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli.errors import CliError, ExitCode
from app.cli.formats import format_bytes, relative_age
from app.cli.naming import CLI_NAME
from app.cli.output import Output
from app.cli.support import (
    DEFAULT_WAIT_TIMEOUT,
    POLL_SECONDS,
    client_of,
    resolve_image,
)

images_app = typer.Typer(help="Disk images instances can be launched from.", no_args_is_help=True)
isos_app = typer.Typer(help="Boot media available to install from.", no_args_is_help=True)

#: Image states that are not going to change on their own.
_IMAGE_SETTLED = {"Available", "Error"}

_IMAGE_STYLES = {"Available": "green", "Importing": "yellow", "Error": "red"}


@images_app.command("ls")
def images_ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List the image catalog."""
    out = Output(json_mode=json_out)
    rows = client_of(ctx).images()

    if json_out:
        out.emit(rows)
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("STATUS")
    table.add_column("FORMAT")
    table.add_column("VIRTUAL", justify="right")
    table.add_column("ON DISK", justify="right")
    table.add_column("CLOUD-INIT")
    table.add_column("SOURCE")
    table.add_column("ID")

    if not rows:
        out.human("[dim]no images[/dim]")
        return

    for row in rows:
        status = row["status"]
        table.add_row(
            escape(row["name"]),
            f"[{_IMAGE_STYLES.get(status, 'white')}]{status}[/]",
            row.get("format") or "—",
            format_bytes(row.get("virtual_size_bytes")),
            format_bytes(row.get("actual_size_bytes")),
            "yes" if row.get("has_cloud_init") else "no",
            row.get("source") or "—",
            row["id"],
        )
    out.human(table)
    for row in rows:
        if row.get("error_message"):
            out.warn(f"{row['name']}: {row['error_message']}")


@images_app.command("import")
def images_import(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Argument(help="Path to the image file, as the backend sees it."),
    ],
    name: Annotated[
        str | None, typer.Option("--name", help="Catalog name. Defaults to the filename.")
    ] = None,
    cloud_init: Annotated[
        bool,
        typer.Option(
            "--cloud-init/--no-cloud-init",
            help="Whether the guest consumes a NoCloud seed. Without it no SSH key "
            "can be injected and instances are console-only.",
        ),
    ] = True,
    wait: Annotated[
        bool, typer.Option("--wait", help="Poll until the import finishes.")
    ] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Register a disk image with the catalog.

    The path is resolved on the backend's filesystem and the file is copied into
    the image store, so the original may be moved or deleted afterwards.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)

    # Sent as an absolute path: the backend resolves it relative to its own
    # working directory, which is rarely the one the user is standing in.
    source = str(Path(path).expanduser().resolve())
    image = client.import_image(
        name or Path(path).stem, source, has_cloud_init=cloud_init
    )

    if not wait:
        if json_out:
            out.emit(image)
        else:
            out.human(f"{image['name']}  {image['id']}  {image['status']}")
            out.note(f"Copying in the background. Follow it with '{CLI_NAME} images ls'.")
        return

    image = _wait_for_image(ctx, out, image["id"], timeout=timeout)
    if json_out:
        out.emit(image)
    else:
        out.human(f"{image['name']} is {image['status']}")


def _wait_for_image(ctx: typer.Context, out: Output, image_id: str, *, timeout: int) -> dict:
    client = client_of(ctx)
    deadline = time.monotonic() + timeout
    with out.spinner("Importing…") as progress:
        while True:
            image = client.image(image_id)
            progress.update(f"Importing {image['name']}: {image['status']}")
            if image["status"] == "Error":
                raise CliError(
                    image.get("error_message") or "The import failed.",
                    ExitCode.FAILURE,
                )
            if image["status"] in _IMAGE_SETTLED:
                return image
            if time.monotonic() >= deadline:
                raise CliError(
                    f"Timed out after {timeout}s waiting for '{image['name']}' to import.",
                    ExitCode.TIMEOUT,
                    hint="The copy continues in the background; check 'images ls'.",
                )
            time.sleep(POLL_SECONDS)


@images_app.command("rm")
def images_rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
) -> None:
    """Delete an image.

    Refused by the API while any live instance is backed by it — every overlay
    keeps a hard reference to its backing file, and removing it underneath one
    corrupts that instance's disk with no way back.
    """
    out = Output()
    client = client_of(ctx)
    image = resolve_image(client, reference)

    out.confirm(
        f"This deletes the image [bold]{escape(image['name'])}[/bold] and its file.",
        assume_yes=yes,
        action="delete",
    )
    client.delete_image(image["id"])
    out.human(f"Deleted {image['name']}")


@isos_app.command("ls")
def isos_ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List boot media the backend can see.

    Files are placed in the ISO directory by hand; there is no upload endpoint,
    so an empty list usually means the directory, not the feature.
    """
    out = Output(json_mode=json_out)
    rows = client_of(ctx).isos()

    if json_out:
        out.emit(rows)
        return
    if not rows:
        out.human("[dim]no boot media[/dim]")
        out.note(
            "Drop .iso files into the backend's ISO directory "
            "(KURUKURU_ISO_DIR, default ~/.kurukuru/isos)."
        )
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("SIZE", justify="right")
    table.add_column("MODIFIED", justify="right")
    for row in rows:
        table.add_row(
            escape(row["name"]),
            format_bytes(row.get("size_bytes")),
            relative_age(row.get("modified_at")),
        )
    out.human(table)
