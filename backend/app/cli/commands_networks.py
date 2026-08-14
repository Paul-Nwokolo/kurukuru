"""
``iaas net`` — networking, and what this build can actually do.

One mode ships: QEMU's user-mode NAT. A guest on it has no address of its own,
so a **port forward is the only way in** — and unlike almost everything else
here, a forward can be added to a *running* instance and starts working
immediately.

``iaas net modes`` prints what the deferred modes would require of the
operator. That is a genuinely useful answer to "why can't I bridge?", not an
apology, so it names the driver and the privilege per platform.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from app.cli.errors import CliError, ExitCode
from app.cli.output import Output
from app.cli.support import client_of, resolve_instance

net_app = typer.Typer(help="Networks and port forwards.", no_args_is_help=True)


@net_app.command("ls")
def ls(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List networks."""
    out = Output(json_mode=json_out)
    networks = client_of(ctx).networks()

    if json_out:
        out.emit(networks)
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("MODE")
    table.add_column("CIDR")
    table.add_column("INSTANCES", justify="right")
    for network in networks:
        table.add_row(
            escape(network["name"]),
            network["mode"],
            network.get("cidr") or "",
            str(network["instance_count"]),
        )
    out.human(table)


@net_app.command("modes")
def modes(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Show which network modes work here, and what the rest would need."""
    out = Output(json_mode=json_out)
    payload = client_of(ctx).network_modes()

    if json_out:
        out.emit(payload)
        return

    for mode in payload["available"]:
        out.human(f"[green]available[/green]  {mode['mode']} - {mode['label']}")
        out.human(f"           {mode['summary']}")
    for mode in payload["deferred"]:
        out.human("")
        out.human(f"[yellow]deferred[/yellow]   {mode['mode']} - {mode['label']}")
        out.human(f"           {mode['summary']}")
        out.human(f"           [dim]why:[/dim] {mode['blocker']}")
        out.human(f"           [dim]needs:[/dim] {mode['requires']}")


@net_app.command("forwards")
def forwards(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List what reaches an instance, SSH included."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)
    rows = client.forwards(instance["id"])

    if json_out:
        out.emit(rows)
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("ID")
    table.add_column("HOST", justify="right")
    table.add_column("GUEST", justify="right")
    table.add_column("PROTO")
    table.add_column("NOTE")
    for row in rows:
        # The SSH forward is derived from the instance's pinned port rather than
        # stored, and cannot be removed. Dimmed, and its id column says so, so
        # nobody tries to delete it and then wonders why it comes back.
        if row.get("derived"):
            table.add_row(
                "[dim]derived[/dim]",
                f"[dim]{row['host_port']}[/dim]",
                f"[dim]{row['guest_port']}[/dim]",
                f"[dim]{row['protocol']}[/dim]",
                f"[dim]{escape(row.get('description') or '')}[/dim]",
            )
        else:
            table.add_row(
                row["id"][:8],
                str(row["host_port"]),
                str(row["guest_port"]),
                row["protocol"],
                escape(row.get("description") or ""),
            )
    out.human(table)
    # Said plainly because the failure it prevents looks like a broken forward:
    # someone exposes a web server, tries it from their phone, and concludes
    # the feature does not work.
    out.human(
        "[dim]Forwards bind to 127.0.0.1 only — reachable from this machine, "
        "not from the LAN.[/dim]"
    )


@net_app.command("forward")
def forward(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    host_port: Annotated[int, typer.Argument(help="Port on this machine.")],
    guest_port: Annotated[int, typer.Argument(help="Port inside the guest.")],
    protocol: Annotated[str, typer.Option("--protocol", help="tcp or udp.")] = "tcp",
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="What it is for.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Forward a host port into an instance. Live — no restart needed."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)

    if protocol not in ("tcp", "udp"):
        raise CliError(f"Unknown protocol '{protocol}'. Use tcp or udp.", ExitCode.USAGE)

    created = client.add_forward(
        instance["id"], host_port, guest_port, protocol=protocol, description=description
    )

    if json_out:
        out.emit(created)
    else:
        out.human(f"127.0.0.1:{host_port} -> {instance['name']}:{guest_port} ({protocol})")
        out.note(
            "Listening now."
            if instance["status"] == "Running"
            else "Takes effect when the instance starts."
        )
        out.note(
            "Bound to 127.0.0.1 only — reachable from this machine, not from the LAN."
        )


@net_app.command("unforward")
def unforward(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="INSTANCE")],
    host_port: Annotated[int, typer.Argument(help="The host port to stop forwarding.")],
) -> None:
    """Remove a port forward."""
    out = Output()
    client = client_of(ctx)
    instance = resolve_instance(client, reference)

    match = next(
        (
            f
            for f in client.forwards(instance["id"])
            if f["host_port"] == host_port and not f.get("derived")
        ),
        None,
    )
    if match is None:
        raise CliError(
            f"'{instance['name']}' has no removable forward on host port {host_port}.",
            ExitCode.NOT_FOUND,
            hint="The SSH forward cannot be removed; it goes with the instance.",
        )

    client.remove_forward(instance["id"], match["id"])
    out.human(f"port {host_port} no longer forwards to {instance['name']}")
