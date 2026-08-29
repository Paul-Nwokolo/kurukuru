"""
``kurukuru projects ls|create|rm`` — grouping, not tenancy.

The `--project` flag on the root command scopes the other commands to one
project. It filters what you see; it grants and withholds nothing, because
there is no authentication for it to hang off. Every message here is written
so nobody reads a permission into it.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from kurukuru.cli.client import ApiClient
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import relative_age
from kurukuru.cli.naming import CLI_NAME
from kurukuru.cli.output import Output
from kurukuru.cli.support import client_of, config_of

projects_app = typer.Typer(
    help="Group instances, images and key pairs. Organisational only — not an "
    "isolation boundary.",
    no_args_is_help=True,
)


def resolve_project(client: ApiClient, reference: str) -> dict:
    """Find one project by id or by name.

    Ids win over names, matching how instances resolve, so a name that happens
    to look like an id cannot shadow the real one.
    """
    projects = client.projects()
    for project in projects:
        if project["id"] == reference:
            return project
    for project in projects:
        if project["name"] == reference:
            return project
    raise CliError(
        f"No project named '{reference}'.",
        ExitCode.NOT_FOUND,
        hint=f"Run '{CLI_NAME} projects ls' to see them.",
    )


def scoped_project_id(ctx: typer.Context, client: ApiClient) -> str | None:
    """The project id the current invocation is scoped to, if any.

    Resolved here rather than in the root callback because it costs an HTTP
    call, and most commands are not scoped — a `--project` nobody passed must
    not make every command slower or fail when the backend is down.
    """
    reference = config_of(ctx).project
    if not reference:
        return None
    return resolve_project(client, reference)["id"]


@projects_app.command("ls")
def ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List projects and what is filed under each."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    projects = client.projects()

    if json_out:
        out.emit(projects)
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("INSTANCES", justify="right")
    table.add_column("IMAGES", justify="right")
    table.add_column("KEYS", justify="right")
    table.add_column("AGE", justify="right")
    table.add_column("DESCRIPTION")
    for project in projects:
        name = escape(project["name"])
        table.add_row(
            f"[bold]{name}[/bold] [dim](default)[/dim]" if project["is_default"] else name,
            str(project["instance_count"]),
            str(project["image_count"]),
            str(project["keypair_count"]),
            relative_age(project.get("created_at")),
            escape(project.get("description") or ""),
        )
    out.human(table)


@projects_app.command("create")
def create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Name for the project.")],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="What it is for.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Create a project."""
    out = Output(json_mode=json_out)
    project = client_of(ctx).create_project(name, description)

    if json_out:
        out.emit(project)
    else:
        out.human(f"{project['name']}  {project['id']}")
        out.note(
            f"File things under it with --project {project['name']}, or "
            f"${'KURUKURU_PROJECT'}."
        )


@projects_app.command("rename")
def rename(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="PROJECT")],
    name: Annotated[str, typer.Argument(help="The new name.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Rename a project. Nothing filed under it moves."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    project = resolve_project(client, reference)
    renamed = client.rename_project(project["id"], name)

    if json_out:
        out.emit(renamed)
    else:
        out.human(f"{project['name']} renamed to {renamed['name']}")


@projects_app.command("rm")
def rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="PROJECT")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
) -> None:
    """Delete a project. Its images and key pairs move to the default one.

    Refused while live instances are filed under it — the API names them.
    Nothing filed under a project is ever deleted with it.
    """
    out = Output()
    client = client_of(ctx)
    project = resolve_project(client, reference)

    kept = project["image_count"] + project["keypair_count"]
    moving = (
        f" Its {kept} image(s) and key pair(s) will be moved to the default project, "
        "not deleted."
        if kept
        else ""
    )
    out.confirm(
        f"This deletes the project [bold]{escape(project['name'])}[/bold].{moving}",
        assume_yes=yes,
        action="delete",
    )

    client.delete_project(project["id"])
    out.human(f"{project['name']} deleted")
