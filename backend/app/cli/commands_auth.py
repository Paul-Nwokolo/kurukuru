"""
``kurukuru auth init|login|logout|whoami|reset-password`` and ``kurukuru auth token …``.

**Two of these deliberately do not use the API.** ``init`` and ``reset-password``
open the database directly, and that is the design rather than a shortcut.

Creating the first account over HTTP would need a public, state-changing route —
one anybody who can reach the port may call, exactly once, to become the owner.
Doing it on the host instead requires filesystem access to the state directory,
which is a *stronger* requirement than any credential this product could check:
whoever can read that directory can already read the orchestrator's SSH private
key and every VM disk in it. So the local operation protects more while adding
no public surface, and the brief's "a CLI command run on the host" is exactly
the shape it recommends for password reset anyway.

Everything else goes through the API like any other client, authenticated with
the token in the token file.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli import auth_store, host_admin
from app.cli.client import ApiClient
from app.cli.errors import CliError, ExitCode
from app.cli.formats import relative_age
from app.cli.naming import CLI_NAME
from app.cli.output import Output
from app.cli.support import client_of

auth_app = typer.Typer(help="Accounts, sessions and API tokens.", no_args_is_help=True)
token_app = typer.Typer(help="Long-lived API tokens for scripts.", no_args_is_help=True)
auth_app.add_typer(token_app, name="token")


# --------------------------------------------------------------------------- #
# Host-local operations: no API, no network, no credential but the filesystem
# --------------------------------------------------------------------------- #
def _prompt_new_password(out: Output) -> str:
    if not out.interactive:
        raise CliError(
            "A password is required and stdin is not a terminal.",
            ExitCode.USAGE,
            hint="Run this from a terminal; the password is never taken as a flag.",
        )
    first = typer.prompt("New password", hide_input=True)
    second = typer.prompt("Confirm password", hide_input=True)
    if first != second:
        raise CliError("The passwords do not match.", ExitCode.USAGE)
    try:
        return host_admin.validate_password(first)
    except ValueError as exc:
        raise CliError(
            str(exc), ExitCode.INVALID,
            hint=f"At least {host_admin.minimum_password_length()} characters. "
                 f"Length is the only rule.",
        ) from exc


@auth_app.command("init")
def init(
    ctx: typer.Context,
    username: Annotated[str, typer.Option("--username", "-u")] = "owner",
) -> None:
    """Create the owner account. Run this once, on the machine hosting the API.

    The password is prompted for, never passed as a flag: a flag lands in shell
    history and in this project's own approved-command list, which is precisely
    the leak documented in CONTRIBUTING.
    """
    out = Output(json_mode=False)
    if host_admin.account_exists():
        raise CliError(
            "This install already has an account.",
            ExitCode.CONFLICT,
            hint=f"Sign in with '{CLI_NAME} auth login', or reset the password "
                 f"with '{CLI_NAME} auth reset-password'.",
        )
    password = _prompt_new_password(out)
    try:
        created = host_admin.create_owner(
            username, password, f"{CLI_NAME} on this host"
        )
    except host_admin.HostAdminError as exc:
        raise CliError(str(exc), ExitCode.CONFLICT) from exc
    secret = created.token

    out.human(f"Created the owner account [bold]{escape(username)}[/bold].")
    _store_and_report(ctx, out, secret)
    out.note(f"Sign in to the dashboard as '{username}' with the password you just set.")


@auth_app.command("reset-password")
def reset_password(
    username: Annotated[str, typer.Option("--username", "-u")] = "owner",
) -> None:
    """Set a new password without knowing the old one. Host-local, like ``init``.

    This is the documented recovery path. It invalidates every session and every
    API token, including the one in this machine's token file — sign in again
    afterwards.
    """
    out = Output(json_mode=False)
    if not host_admin.account_exists():
        raise CliError(
            "This install has no account yet.", ExitCode.NOT_FOUND,
            hint=f"Run '{CLI_NAME} auth init' to create the owner account.",
        )
    password = _prompt_new_password(out)
    try:
        host_admin.reset_password(username, password)
    except host_admin.HostAdminError as exc:
        raise CliError(
            str(exc), ExitCode.NOT_FOUND,
            hint=f"Run '{CLI_NAME} auth init' if this install has no account yet.",
        ) from exc

    auth_store.clear()
    out.human(f"Password reset for [bold]{escape(username)}[/bold].")
    out.warn("Every session and API token has been invalidated, including this "
             f"machine's. Run '{CLI_NAME} auth login' to sign in again.")


# --------------------------------------------------------------------------- #
# API-backed operations
# --------------------------------------------------------------------------- #
def _store_and_report(ctx: typer.Context, out: Output, secret: str) -> None:
    """Save the token and say honestly who can read it."""
    api_url = getattr(ctx.obj, "api_url", "") if ctx.obj else ""
    path, protected, detail = auth_store.save(secret, api_url)
    out.human(f"Token stored at {path}")
    if protected:
        out.note(f"Readable only by your account ({detail}).")
    else:
        out.warn(
            f"This file could NOT be restricted: {detail} "
            f"Anyone able to read it can use this token until you revoke it with "
            f"'{CLI_NAME} auth token rm'."
        )


@auth_app.command("login")
def login(
    ctx: typer.Context,
    username: Annotated[str, typer.Option("--username", "-u")] = "owner",
    name: Annotated[
        str | None, typer.Option("--token-name", help="Label for the token created.")
    ] = None,
) -> None:
    """Sign in and store an API token for this machine.

    The password buys a session, the session mints a token, and the token is
    what the CLI keeps — so the password is never written anywhere.
    """
    out = Output(json_mode=False)
    client: ApiClient = client_of(ctx)

    if not out.interactive:
        raise CliError(
            "A password is required and stdin is not a terminal.", ExitCode.USAGE,
            hint="Create a token from a terminal, then set it in the environment "
                 "for scripts.",
        )
    password = typer.prompt(f"Password for {username}", hide_input=True)

    session = client.login(username, password)
    try:
        created = client.create_token_with_csrf(
            session["csrf"], name or f"{CLI_NAME} on this host"
        )
    finally:
        client.logout_with_csrf(session["csrf"])

    out.human(f"Signed in as [bold]{escape(session['user']['username'])}[/bold].")
    _store_and_report(ctx, out, created["token"])


@auth_app.command("logout")
def logout(ctx: typer.Context) -> None:
    """Revoke this machine's stored token and forget it."""
    out = Output(json_mode=False)
    stored = auth_store.load()
    if stored is None:
        out.human("Not signed in.")
        return

    client: ApiClient = client_of(ctx)
    revoked = False
    try:
        mine = client.request("GET", "/auth/tokens")
        # The stored secret is not recoverable from the list, so match on the
        # prefix it was created with — unique in practice, and the worst case is
        # revoking one token too few, which `auth token rm` fixes.
        for row in mine:
            if stored.token.startswith(row["prefix"]) and row["revoked_at"] is None:
                client.request("DELETE", f"/auth/tokens/{row['id']}")
                revoked = True
                break
    except CliError as exc:
        out.warn(f"Could not revoke the token on the server: {exc}")

    auth_store.clear()
    out.human("Signed out." if revoked else "Local token removed.")
    if not revoked:
        out.note("The token was not revoked on the server — revoke it with "
                 f"'{CLI_NAME} auth token ls' and 'rm' if it may have leaked.")


@auth_app.command("whoami")
def whoami(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report the account this machine's token belongs to."""
    out = Output(json_mode=json_out)
    client: ApiClient = client_of(ctx)
    me = client.request("GET", "/auth/whoami")

    if json_out:
        out.emit(me)
        return
    out.human(f"{me['username']}{'  (owner)' if me['is_owner'] else ''}")
    stored = auth_store.load()
    if stored:
        out.note(f"Using the token at {auth_store.token_path()} ({stored.redacted})")


@token_app.command("ls")
def token_ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List API tokens. Secrets are not stored and cannot be shown again."""
    out = Output(json_mode=json_out)
    rows = client_of(ctx).request("GET", "/auth/tokens")

    if json_out:
        out.emit(rows)
        return
    if not rows:
        out.human("[dim]no tokens[/dim]")
        return
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("PREFIX")
    table.add_column("CREATED", justify="right")
    table.add_column("LAST USED", justify="right")
    table.add_column("STATE")
    for row in rows:
        revoked = row["revoked_at"] is not None
        table.add_row(
            escape(row["name"]),
            row["prefix"],
            relative_age(row["created_at"]),
            relative_age(row["last_used_at"]) if row["last_used_at"] else "never",
            "[red]revoked[/]" if revoked else "[green]active[/]",
        )
    out.human(table)


@token_app.command("create")
def token_create(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="What this token is for.")],
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a token and print it once.

    Printed to stdout rather than stored: this one is for another machine or a
    script, and writing it into *this* machine's token file would replace the
    credential the CLI is using.
    """
    out = Output(json_mode=json_out)
    created = client_of(ctx).request("POST", "/auth/tokens", json={"name": name})

    if json_out:
        out.emit(created)
        return
    out.human(created["token"])
    out.note("Shown once. It is stored hashed and cannot be displayed again.")


@token_app.command("rm")
def token_rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="TOKEN", help="Name, prefix or id.")],
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Revoke a token. It stops working on the next request."""
    out = Output(json_mode=False)
    client = client_of(ctx)
    rows = client.request("GET", "/auth/tokens")
    matches = [
        r for r in rows
        if reference in (r["id"], r["name"], r["prefix"]) and r["revoked_at"] is None
    ]
    if not matches:
        raise CliError(
            f"No active token matching '{reference}'.", ExitCode.NOT_FOUND,
            hint=f"Run '{CLI_NAME} auth token ls' to see them.",
        )
    if len(matches) > 1:
        raise CliError(
            f"'{reference}' matches {len(matches)} tokens.", ExitCode.CONFLICT,
            hint="Pass the id or the prefix instead.",
        )

    row = matches[0]
    out.confirm(
        f"This revokes [bold]{escape(row['name'])}[/bold] ({row['prefix']}). "
        f"Anything using it stops working immediately.",
        assume_yes=yes,
        action="revoke",
    )
    client.request("DELETE", f"/auth/tokens/{row['id']}")
    out.human(f"Revoked {row['name']}")
    stored = auth_store.load()
    if stored and stored.token.startswith(row["prefix"]):
        auth_store.clear()
        out.warn(f"That was this machine's token. Run '{CLI_NAME} auth login' again.")
