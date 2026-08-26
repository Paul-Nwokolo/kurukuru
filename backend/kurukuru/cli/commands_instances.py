"""
Instance lifecycle commands: launch, ls, show, start, stop, rm, ssh, console.

These mirror the API's semantics rather than smoothing them over. ``POST
/instances`` returns 202 and provisions in the background, so ``launch`` without
``--wait`` prints an id and exits — the same shape the HTTP caller sees. With
``--wait`` the CLI does the polling the API asks callers to do, and reports the
outcome as an exit code so a shell can branch on it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from kurukuru.cli.client import ApiClient
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import (
    absolute_time,
    format_memory,
    parse_disk_gb,
    parse_memory_mb,
    relative_age,
    status_text,
)
from kurukuru.cli.naming import CLI_NAME
from kurukuru.cli.output import Output
from kurukuru.cli.commands_projects import scoped_project_id
from kurukuru.cli.support import (
    DEFAULT_WAIT_TIMEOUT,
    client_of,
    config_of,
    image_names,
    resolve_image,
    resolve_instance,
    source_label,
    wait_for_instance,
)


#: How long `ssh` waits for the guest's SSH service before connecting anyway.
#: Long enough to ride out cloud-init restarting sshd on a first boot, short
#: enough that a genuinely unreachable guest is reported promptly.
SSH_READY_TIMEOUT = 30.0

#: Where ssh is told to keep host keys it must not keep. Deliberately the POSIX
#: spelling on **every** platform, including Windows — this is not an oversight.
#:
#: ``os.devnull`` is "nul" on Windows, and which ssh the user has decides what
#: that means. Measured on a Windows 11 host against a live guest:
#:
#:     UserKnownHostsFile=  | MSYS ssh (Git Bash) | Win32 OpenSSH
#:     nul  (= os.devnull)  | creates ./nul       | discards
#:     NUL                  | creates ./NUL       | discards
#:     /dev/null            | discards            | discards
#:
#: Git Bash's ssh is a POSIX build: it has no notion of a reserved device name,
#: so it takes "nul" as an ordinary relative filename and drops a known_hosts
#: file wherever the user happened to be standing. Win32 OpenSSH understands
#: "/dev/null" — so the POSIX spelling is the only one that is correct in all
#: three environments, and the platform-aware constant is the wrong tool here.
NULL_DEVICE = "/dev/null"


def register(app: typer.Typer) -> None:
    """Attach every instance command to the root app."""
    app.command("launch")(launch)
    app.command("ls")(ls)
    app.command("show")(show)
    app.command("start")(start)
    app.command("stop")(stop)
    app.command("rm")(rm)
    app.command(
        "ssh",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(ssh)
    app.command("console")(console)


# --------------------------------------------------------------------------- #
# launch
# --------------------------------------------------------------------------- #
def launch(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Instance name (a-z, digits, hyphens).")],
    preset: Annotated[
        str | None,
        typer.Option("--preset", help="Sizing preset to start from: small, medium, large."),
    ] = None,
    cpus: Annotated[int | None, typer.Option("--cpus", help="vCPU count.")] = None,
    memory: Annotated[
        str | None,
        typer.Option("--memory", help="Memory: 2G, 2048M, or a plain number of MB."),
    ] = None,
    disk: Annotated[
        str | None,
        typer.Option("--disk", help="Disk: 20G, or a plain number of GB."),
    ] = None,
    mode: Annotated[
        str | None,
        typer.Option(
            "--mode",
            help="quick (built-in cloud image), iso (install from boot media), "
            "or image (a catalog image). Inferred from --iso/--image when omitted.",
        ),
    ] = None,
    iso: Annotated[
        str | None, typer.Option("--iso", help="Boot media filename; implies --mode iso.")
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Image name or id; implies --mode image."),
    ] = None,
    accel: Annotated[
        str | None, typer.Option("--accel", help="auto, whpx or tcg.")
    ] = None,
    display: Annotated[
        str | None,
        typer.Option("--display", help="std or virtio (virtio renders text-mode guests)."),
    ] = None,
    wait: Annotated[
        bool, typer.Option("--wait", help="Poll until the instance is Running or Error.")
    ] = False,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds to wait with --wait.")
    ] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Provision a new instance.

    Without --wait this returns as soon as the request is accepted, printing the
    id and status — the API provisions in the background and this mirrors it.
    With --wait it polls to Running, and exits non-zero if the instance lands in
    Error, printing the reason the backend recorded.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)

    payload = _launch_payload(
        client,
        name=name,
        preset=preset,
        cpus=cpus,
        memory=memory,
        disk=disk,
        mode=mode,
        iso=iso,
        image=image,
        accel=accel,
        display=display,
        project_id=scoped_project_id(ctx, client),
    )

    instance = client.create_instance(payload)

    if not wait:
        if json_out:
            out.emit(instance)
        else:
            out.human(f"{instance['name']}  {instance['id']}  {instance['status']}")
            out.note(f"Provisioning in the background. Follow it with '{CLI_NAME} show {name}'.")
        return

    instance = wait_for_instance(
        client,
        out,
        instance["id"],
        targets={"Running"},
        timeout=timeout,
        label=f"Launching {name}",
    )

    if json_out:
        out.emit(instance)
    else:
        _print_launch_summary(out, instance)


def _launch_payload(client: ApiClient, **options: object) -> dict:
    """Turn command-line options into the API's request body.

    The intent modes are resolved here rather than pushed to the server: the
    API has no notion of "quick" — it has an ISO field and an image field — and
    the mode exists so a user can say what they *meant* and be told when the
    flags disagree with it, instead of getting a surprising default.
    """
    name = str(options["name"])
    mode = options["mode"]
    iso = options["iso"]
    image = options["image"]

    if mode is None:
        mode = "iso" if iso else ("image" if image else "quick")
    if mode not in ("quick", "iso", "image"):
        raise CliError(
            f"Unknown mode '{mode}'. Use quick, iso or image.", ExitCode.USAGE
        )
    if mode == "iso" and not iso:
        raise CliError("--mode iso needs --iso NAME.", ExitCode.USAGE,
                       hint=f"Run '{CLI_NAME} isos ls' to see the available media.")
    if mode == "image" and not image:
        raise CliError("--mode image needs --image NAME_OR_ID.", ExitCode.USAGE,
                       hint=f"Run '{CLI_NAME} images ls' to see the catalog.")
    if mode == "quick" and (iso or image):
        raise CliError(
            "--mode quick launches the built-in cloud image; drop --iso/--image "
            "or name the matching mode.",
            ExitCode.USAGE,
        )
    if iso and image:
        raise CliError("--iso and --image are alternatives; pick one.", ExitCode.USAGE)

    payload: dict[str, object] = {"name": name}
    if options["preset"]:
        payload["preset"] = options["preset"]
    if options["cpus"] is not None:
        payload["cpus"] = options["cpus"]
    if options["memory"] is not None:
        payload["memory_mb"] = parse_memory_mb(str(options["memory"]))
    if options["disk"] is not None:
        payload["disk_gb"] = parse_disk_gb(str(options["disk"]))
    if options["accel"]:
        payload["accel"] = options["accel"]
    if options["display"]:
        payload["display"] = options["display"]
    # A launch lands in the scoped project, so `--project client-a launch web`
    # files it where the user is already working. Unscoped means the default,
    # decided server-side rather than guessed here.
    if options.get("project_id") is not None:
        payload["project_id"] = options["project_id"]

    if mode == "iso":
        payload["iso"] = iso
    elif mode == "image":
        # Resolved to an id here so a user can pass the name they see in
        # `images ls`; the API only accepts ids.
        payload["image_id"] = resolve_image(client, str(image))["id"]

    return payload


def _print_launch_summary(out: Output, instance: dict) -> None:
    out.human(f"[green]{instance['name']} is Running[/green]")
    if instance.get("ip_address") and instance.get("ssh_port"):
        out.human(f"  address   {instance['ip_address']}:{instance['ssh_port']}")
        out.human(f"  ssh       {CLI_NAME} ssh {instance['name']}")
    elif not instance.get("ssh_enabled"):
        # An ISO guest or an image without cloud-init: no key was injected, so
        # saying nothing about access would leave the user hunting for an IP
        # that is never going to appear.
        out.human(f"  access    console only — {CLI_NAME} console {instance['name']}")
    if instance.get("console_caveat"):
        out.warn(instance["console_caveat"])


# --------------------------------------------------------------------------- #
# ls
# --------------------------------------------------------------------------- #
def ls(
    ctx: typer.Context,
    all_instances: Annotated[
        bool, typer.Option("--all", "-a", help="Include terminated instances.")
    ] = False,
    watch: Annotated[
        bool, typer.Option("--watch", "-w", help="Re-render until interrupted.")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", help="Seconds between redraws with --watch.")
    ] = 2.0,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """List instances."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)

    if watch:
        if json_out:
            raise CliError(
                "--watch and --json are incompatible: a stream of documents is not "
                "a JSON document.",
                ExitCode.USAGE,
                hint=f"Loop in the shell instead: while true; do {CLI_NAME} ls --json; sleep 5; done",
            )
        if interval <= 0:
            raise CliError("--interval must be positive.", ExitCode.USAGE)
        _watch_instances(client, out, all_instances, interval)
        return

    # Scoped by --project / $KURUKURU_PROJECT when one is set; unset means every
    # project, which is what `ls` did before projects existed.
    rows = client.instances(
        include_terminated=all_instances, project_id=scoped_project_id(ctx, client)
    )
    if json_out:
        out.emit(rows)
        return
    out.human(_instances_table(rows, image_names(client)))


def _instances_table(rows: list[dict], images: dict[str, str]) -> Table:
    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("NAME")
    table.add_column("STATUS")
    table.add_column("ADDRESS")
    table.add_column("SIZE")
    table.add_column("SOURCE")
    table.add_column("AGE", justify="right")

    if not rows:
        table.add_row("[dim]no instances[/dim]", "", "", "", "", "")
        return table

    for row in rows:
        address = "—"
        if row.get("ip_address"):
            # A QEMU guest is reached through a host port forward, so the port
            # is part of the address — the same rule the dashboard follows.
            address = (
                f"{row['ip_address']}:{row['ssh_port']}"
                if row.get("ssh_port")
                else row["ip_address"]
            )
        size = "—"
        if row.get("cpus") and row.get("memory_mb"):
            size = f"{row['cpus']}c/{format_memory(row['memory_mb'])}"
            if row.get("disk_gb"):
                size += f"/{row['disk_gb']}G"
        table.add_row(
            escape(row["name"]),
            status_text(row),
            address,
            size,
            escape(source_label(row, images)),
            relative_age(row.get("created_at")),
        )
    return table


def _watch_instances(client: ApiClient, out: Output, all_instances: bool, interval: float) -> None:
    """Re-render on an interval until interrupted.

    Ctrl-C is a clean exit, not a traceback: watching is a read, and stopping a
    read is the user finishing, not the tool failing.
    """
    import time

    from rich.live import Live

    try:
        if not out.stdout_is_tty:
            # No cursor to move: print successive snapshots instead of trying
            # to repaint a stream that has no concept of a screen.
            while True:
                rows = client.instances(include_terminated=all_instances)
                out.human(_instances_table(rows, image_names(client)))
                out.human("")
                time.sleep(interval)

        with Live(console=out.out, auto_refresh=False, screen=False) as live:
            while True:
                rows = client.instances(include_terminated=all_instances)
                live.update(_instances_table(rows, image_names(client)), refresh=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        out.note("Stopped.")


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #
def show(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Show everything known about one instance."""
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference, include_terminated=True)
    # Re-read by id: the listing is a snapshot, and between resolving a name and
    # printing it the reconciler may have moved the row on.
    instance = client.instance(instance["id"])

    if json_out:
        out.emit(instance)
        return

    images = image_names(client)
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(style="dim")
    table.add_column()

    def field(label: str, value: object) -> None:
        table.add_row(label, "—" if value in (None, "") else escape(str(value)))

    size = (
        f"{instance['cpus']} vCPU · {format_memory(instance.get('memory_mb'))}"
        f" · {instance.get('disk_gb')} GB"
        if instance.get("cpus")
        else None  # pre-Phase-7 rows the migration could not expand
    )

    field("name", instance["name"])
    field("id", instance["id"])
    table.add_row("status", status_text(instance))
    field("engine", instance.get("engine"))
    field("size", size)
    field("preset", instance.get("flavor"))
    field("source", source_label(instance, images))
    field("address", instance.get("ip_address"))
    field("ssh port", instance.get("ssh_port"))
    field("vnc port", instance.get("vnc_port"))
    field("qmp port", instance.get("qmp_port"))
    field("pid", instance.get("pid"))
    field("accel", instance.get("accel"))
    field("display", instance.get("display"))
    field("ssh enabled", instance.get("ssh_enabled"))
    field("ssh user", instance.get("ssh_user"))
    field("console", "supported" if instance.get("console_supported") else "not available")
    field("created", absolute_time(instance.get("created_at")))
    field("updated", absolute_time(instance.get("updated_at")))
    out.human(table)

    # API-authored text is escaped before it meets rich: a qemu error message
    # containing brackets is not markup, and rendering it as such would eat
    # part of the explanation.
    if instance.get("degraded_reason"):
        out.human(f"\n[dark_orange]degraded:[/] {escape(instance['degraded_reason'])}")
    if instance.get("console_caveat"):
        out.human(f"\n[yellow]console caveat:[/] {escape(instance['console_caveat'])}")
    if instance.get("error_message"):
        out.human(f"\n[red]error:[/] {escape(instance['error_message'])}")


# --------------------------------------------------------------------------- #
# start / stop
# --------------------------------------------------------------------------- #
def start(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    wait: Annotated[bool, typer.Option("--wait", help="Poll until Running.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Start a stopped instance."""
    _transition(
        ctx,
        reference,
        action="start",
        target="Running",
        wait=wait,
        timeout=timeout,
        json_out=json_out,
    )


def stop(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    wait: Annotated[bool, typer.Option("--wait", help="Poll until Stopped.")] = False,
    timeout: Annotated[int, typer.Option("--timeout")] = DEFAULT_WAIT_TIMEOUT,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Stop a running instance (ACPI shutdown, then force after a grace period)."""
    _transition(
        ctx,
        reference,
        action="stop",
        target="Stopped",
        wait=wait,
        timeout=timeout,
        json_out=json_out,
    )


def _transition(
    ctx: typer.Context,
    reference: str,
    *,
    action: str,
    target: str,
    wait: bool,
    timeout: int,
    json_out: bool,
) -> None:
    """Shared body of start and stop.

    Both API routes block until the hypervisor has done the work, so ``--wait``
    is usually a no-op that confirms what already happened. It is still offered
    because the row is only *authoritative* after the reconciler has folded the
    hypervisor's view back in, and a script that immediately SSHes wants that
    guarantee rather than the response body.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)

    call = client.start_instance if action == "start" else client.stop_instance
    gerund = "Starting" if action == "start" else "Stopping"
    with out.spinner(f"{gerund} {instance['name']}…"):
        # The API's 409 already names the blocking state ("Cannot stop an
        # instance in state 'Stopped'"), and that message is better than
        # anything invented here — it is passed through by ApiClient.
        updated = call(instance["id"])

    if wait:
        updated = wait_for_instance(
            client,
            out,
            updated["id"],
            targets={target},
            timeout=timeout,
            label=f"Waiting for {instance['name']} to be {target}",
        )

    if json_out:
        out.emit(updated)
    else:
        out.human(f"{updated['name']} is {updated['status']}")


# --------------------------------------------------------------------------- #
# rm
# --------------------------------------------------------------------------- #
def rm(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Clear the record even if the hypervisor refuses to destroy the VM.",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Terminate an instance: destroy the VM and purge its disk.

    Asks for confirmation when stdout is a terminal. When it is not — a script,
    a pipeline, CI — it does not ask, because a prompt nobody can answer is a
    hang; pass --yes instead.
    """
    out = Output(json_mode=json_out)
    client = client_of(ctx)
    instance = resolve_instance(client, reference, include_terminated=True)

    if instance["status"] == "Terminated":
        out.note(f"'{instance['name']}' is already terminated.")
        if json_out:
            out.emit(instance)
        return

    out.confirm(
        f"This permanently destroys [bold]{instance['name']}[/bold] "
        f"({instance['status']}) and its disk.",
        assume_yes=yes,
        action="terminate",
    )

    with out.spinner(f"Terminating {instance['name']}…"):
        terminated = client.delete_instance(instance["id"], force=force)

    if json_out:
        out.emit(terminated)
    else:
        out.human(f"{terminated['name']} is {terminated['status']}")


# --------------------------------------------------------------------------- #
# ssh
# --------------------------------------------------------------------------- #
def ssh(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Keep OpenSSH's host-key checking instead of bypassing it "
            "for loopback VMs (see the command help).",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            help="Seconds to wait for the guest's SSH service before connecting anyway.",
        ),
    ] = SSH_READY_TIMEOUT,
) -> None:
    """SSH into an instance. Arguments after -- are passed to ssh.

    Builds the same command the dashboard's Copy SSH button produces — the
    orchestrator's key, the forwarded port, the cloud-init user — and hands the
    terminal over to ssh itself. Everything after ``--`` goes to ssh unchanged,
    so `kurukuru ssh web-01 -- uname -a` runs one remote command and exits with its
    status.

    Host-key checking is bypassed by default, and that is not laziness. Every
    VM on this host is reached as 127.0.0.1:<forwarded port>, the port pool is
    small and ports are recycled, so the *same* address legitimately presents a
    different key for every instance that ever holds it. Left to its defaults
    ssh would prompt on the first connection — hanging `launch --wait && ssh`
    in a script — and then hard-refuse with REMOTE HOST IDENTIFICATION HAS
    CHANGED for every instance after it. What the check would defend against is
    an attacker on loopback, who by definition already owns this machine.
    Pass --strict to keep the defaults anyway.
    """
    out = Output()
    client = client_of(ctx)
    instance = resolve_instance(client, reference)
    command = _ssh_command(client, instance, strict=strict) + list(ctx.args)

    wait_for_ssh_service(out, instance["ip_address"], int(instance["ssh_port"]), timeout)
    out.note(" ".join(command))
    _exec(command)


def _ssh_banner(host: str, port: int, timeout: float = 2.0) -> str | None:
    """The guest's SSH identification string, or None if it isn't serving.

    QEMU's user-mode stack accepts a connection on a forwarded port whether or
    not anything is listening behind it, so "the port answers" proves nothing.
    Only the banner does.
    """
    try:
        with socket.create_connection((host, port), timeout) as sock:
            sock.settimeout(timeout)
            data = sock.recv(256)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    return text if text.startswith("SSH-") else None


def wait_for_ssh_service(out: Output, host: str, port: int, timeout: float) -> bool:
    """Hold until the guest is really answering SSH, within a budget.

    This exists because of a race on a cloud image's *first* boot: sshd starts,
    the backend sees its banner and reports Running, and then cloud-init
    regenerates the host keys and restarts sshd. Connections landing in that
    window are accepted and then dropped — ssh calls it
    "kex_exchange_identification: read: Connection aborted" — which is exactly
    what `launch --wait && ssh` would hit, and it has been observed here.

    Waiting for the banner (not just for the port) closes it: during the
    restart the socket answers with EOF instead of an identification string, so
    this keeps waiting where a port check would sail through.

    Returns whether the guest answered. It never *blocks* the connection:
    exhausting the budget still runs ssh, because ssh's own diagnosis of why it
    could not connect beats a message invented here.
    """
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        if _ssh_banner(host, port):
            return True
        if time.monotonic() >= deadline:
            out.warn(
                f"{host}:{port} is not answering SSH after {timeout:g}s; connecting anyway."
            )
            return False
        if not announced:
            out.note("Waiting for the guest's SSH service…")
            announced = True
        time.sleep(1.0)


def _ssh_command(client: ApiClient, instance: dict, *, strict: bool = False) -> list[str]:
    """The argv for reaching this instance, or a refusal explaining why not."""
    if instance.get("ssh_enabled") is False or instance.get("boot_source") == "iso":
        raise CliError(
            f"'{instance['name']}' has no SSH access: no key was injected "
            "(ISO installs and images without cloud-init cannot receive one).",
            ExitCode.CONFLICT,
            hint=f"Use '{CLI_NAME} console {instance['name']}' instead.",
        )
    if instance["status"] != "Running":
        raise CliError(
            f"'{instance['name']}' is {instance['status']}, not Running.",
            ExitCode.CONFLICT,
            hint=f"Start it with '{CLI_NAME} start {instance['name']} --wait'.",
        )
    if not instance.get("ip_address") or not instance.get("ssh_port"):
        raise CliError(
            f"'{instance['name']}' has no address yet — it is Running but has not "
            "published one.",
            ExitCode.CONFLICT,
            hint=f"'{CLI_NAME} show {instance['name']}' reports whether it is degraded; "
            "cloud-init may still be running.",
        )

    # Every VM trusts exactly one key: the orchestrator's. Without -i, ssh
    # offers whatever the user's agent holds and the guest answers "Permission
    # denied (publickey)".
    key_path = client.ssh_key().get("private_key_path")
    command = ["ssh"]
    if key_path:
        command += ["-i", str(key_path)]
    if not strict:
        # See the command docstring: recycled loopback ports mean the same
        # address legitimately changes key, so known_hosts is written to the
        # bit bucket rather than accumulating entries that will later collide.
        command += [
            "-o", f"UserKnownHostsFile={NULL_DEVICE}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "LogLevel=ERROR",  # otherwise every run prints a key warning
        ]
    command += ["-p", str(instance["ssh_port"])]
    command.append(f"{instance.get('ssh_user', 'iaas')}@{instance['ip_address']}")
    return command


def _exec(command: list[str]) -> None:
    """Hand the terminal to ssh.

    On POSIX the process is *replaced*: ssh then owns the tty directly, so
    job control, Ctrl-C and the exit status all behave as if it had been typed.
    Windows has no execve that survives a shell, so there the child is run to
    completion and its status propagated instead.
    """
    if os.name == "posix":
        # execvp never returns, so anything still sitting in a buffer is gone.
        # The note above ("ssh -i ...") is written through rich and would be
        # lost exactly when it is most wanted — a redirected run that failed.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execvp(command[0], command)
        except FileNotFoundError as exc:
            raise CliError(
                "ssh was not found on PATH.",
                ExitCode.FAILURE,
                hint="Install an OpenSSH client.",
            ) from exc
        # Unreachable: a successful execvp does not return. Stated explicitly
        # so the POSIX branch is terminal — without it, control would fall into
        # the Windows path below and run ssh a second time as a child.
        raise CliError("ssh could not be started.", ExitCode.FAILURE)

    try:
        completed = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise CliError(
            "ssh was not found on PATH.",
            ExitCode.FAILURE,
            hint="Install the OpenSSH Client optional feature (Windows 10+ ships it).",
        ) from exc
    sys.exit(completed.returncode)


# --------------------------------------------------------------------------- #
# console
# --------------------------------------------------------------------------- #
def console(
    ctx: typer.Context,
    reference: Annotated[str, typer.Argument(metavar="NAME_OR_ID")],
    print_url: Annotated[
        bool, typer.Option("--print", help="Print the URL instead of opening a browser.")
    ] = False,
) -> None:
    """Open this instance's console in the dashboard.

    The framebuffer arrives over a WebSocket the browser already knows how to
    render; a terminal VNC client is out of scope, so this hands off to the
    dashboard with a deep link rather than reimplementing it badly.
    """
    out = Output()
    cfg = config_of(ctx)
    client = client_of(ctx)
    instance = resolve_instance(client, reference)

    if not instance.get("console_supported"):
        raise CliError(
            f"'{instance['name']}' has no console: it is {instance['status']} and "
            "no accelerator has been resolved for it yet.",
            ExitCode.CONFLICT,
            hint="A console exists once the VM has been launched.",
        )
    if instance.get("console_caveat"):
        out.warn(instance["console_caveat"])

    url = f"{cfg.dashboard_url}/?console={instance['id']}"
    out.human(url)
    if print_url:
        return
    if not webbrowser.open(url):
        out.warn("No browser could be opened; the URL above is the console.")
