"""
System commands: serve, capacity, doctor, version, completion.

``doctor`` is the centre of gravity here. It is the first thing a confused user
runs, so every line it prints answers two questions — what is wrong, and what to
do about it — and the remedies are written for someone who has never seen this
project before.

The facts it reports come from the API (``/health``, ``/diagnostics``). That is
not ceremony: the checks are about the machine the *backend* runs on, and a CLI
that probed its own filesystem would confidently describe the wrong host the
moment those differ.
"""

from __future__ import annotations

import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from kurukuru.cli.client import ApiClient
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import format_bytes, format_memory
from kurukuru.cli.naming import CLI_NAME, cli_version, env_var
from kurukuru.cli.output import Output
from kurukuru.cli.support import client_of, config_of

#: Below this, an instance store is close enough to full to be worth a warning:
#: the Ubuntu base image alone is ~600 MB and every instance adds an overlay.
_LOW_DISK_GB = 10

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_CHECK_STYLES = {PASS: "green", WARN: "yellow", FAIL: "red"}


def register(app: typer.Typer) -> None:
    app.command("serve")(serve)
    app.command("capacity")(capacity)
    app.command("doctor")(doctor)
    app.command("version")(version)
    app.command("completion")(completion)


# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #
def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind.")] = 8000,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart on source changes (development).")
    ] = False,
) -> None:
    """Run the backend in the foreground.

    A convenience wrapper around uvicorn, not a process manager: no daemonising,
    no PID file, no restart policy. Stopping it is Ctrl-C. Installing the
    backend as a service is a later phase.

    Binds loopback by default. The API has no authentication yet, so exposing it
    on a LAN interface would publish unauthenticated control of every VM on the
    host — pass --host explicitly if you accept that.
    """
    import uvicorn

    import app as app_package

    # The backend resolves its SQLite URL ("sqlite:///./kurukuru.db") and its .env
    # against the working directory. Started from anywhere else, it would create
    # a second, empty database and report no instances — so the directory is
    # pinned to the package's own, exactly as the documented uvicorn invocation
    # assumes.
    backend_dir = Path(app_package.__file__).resolve().parent.parent
    out = Output()
    out.note(f"Serving {backend_dir} on http://{host}:{port} (Ctrl-C to stop)")
    os.chdir(backend_dir)

    uvicorn.run("kurukuru.main:app", host=host, port=port, reload=reload)


# --------------------------------------------------------------------------- #
# capacity
# --------------------------------------------------------------------------- #
def capacity(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """What the host can still give a new instance.

    Three numbers per resource, because a limit you cannot see the arithmetic
    behind is not actionable: what exists, what is already spoken for, and what
    is left.
    """
    out = Output(json_mode=json_out)
    data = client_of(ctx).capacity()

    if json_out:
        out.emit(data)
        return

    table = Table(box=None, pad_edge=False, header_style="dim")
    table.add_column("RESOURCE")
    table.add_column("TOTAL", justify="right")
    table.add_column("COMMITTED", justify="right")
    table.add_column("ALLOCATABLE", justify="right")
    table.add_column("MAX/INSTANCE", justify="right")

    cpu = data["cpu"]
    table.add_row(
        "vCPU",
        str(cpu["total"]),
        str(cpu["committed"]),
        str(cpu["allocatable"]),
        str(cpu["max_per_instance"]),
    )
    memory = data["memory_mb"]
    table.add_row(
        "memory",
        format_memory(memory["total"]),
        format_memory(memory["committed"]),
        format_memory(memory["allocatable"]),
        format_memory(memory["max_per_instance"]),
    )
    disk = data["disk_gb"]
    table.add_row(
        "disk",
        f"{disk['total']}G",
        f"{disk['committed']}G",
        f"{disk['allocatable']}G",
        f"{disk['max_per_instance']}G",
    )
    out.human(table)
    out.human(
        f"\n[dim]accelerator[/dim] {data.get('accel') or 'none'}"
        f"   [dim]host memory free[/dim] {format_memory(data.get('memory_available_mb'))}"
    )
    # vCPUs timeshare, so an allocatable figure above the core count is correct
    # rather than a bug — say so before someone reports it as one.
    if cpu["allocatable"] > cpu["total"]:
        out.human(
            "[dim]vCPU allocatable exceeds the core count because vCPUs are "
            "oversubscribed; no single instance may exceed MAX/INSTANCE.[/dim]"
        )
    if data.get("degraded"):
        out.warn("Capacity limits are not being enforced:")
    for warning in data.get("warnings", []):
        out.warn(warning)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    """One diagnostic line: a verdict, what was seen, and what to do."""

    name: str
    status: str
    detail: str
    remedy: str | None = None


def doctor(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Check that everything this tool depends on is actually working.

    Exits 3 if the API cannot be reached (nothing else can be checked without
    it), 1 if any check FAILs, and 0 otherwise. WARN never fails the run: a
    missing accelerator is slow, not broken.
    """
    out = Output(json_mode=json_out)
    cfg = config_of(ctx)
    client = client_of(ctx)

    checks: list[Check] = [_check_cli()]
    unreachable = False

    try:
        health = client.health()
        checks.insert(
            0,
            Check(
                "API",
                PASS,
                f"{health.get('service', 'backend')} at {cfg.api_url}",
            ),
        )
    except CliError as exc:
        unreachable = exc.code is ExitCode.UNREACHABLE
        checks.insert(
            0,
            Check(
                "API",
                FAIL,
                exc.message,
                f"Start the backend with '{CLI_NAME} serve'. If it runs elsewhere, "
                f"set ${env_var('API_URL')} or pass --api-url.",
            ),
        )

    if not unreachable:
        checks.extend(_backend_checks(client))

    _render_doctor(out, checks)

    if unreachable:
        raise typer.Exit(ExitCode.UNREACHABLE)
    if any(check.status == FAIL for check in checks):
        raise typer.Exit(ExitCode.FAILURE)


def _check_cli() -> Check:
    """The CLI's own runtime. Cheap, and rules out the boring explanation."""
    python = platform.python_version()
    supported = tuple(int(part) for part in python.split(".")[:2]) >= (3, 11)
    return Check(
        f"{CLI_NAME} CLI",
        PASS if supported else FAIL,
        f"v{cli_version()} on Python {python}",
        None if supported else "This project needs Python 3.11 or newer.",
    )


#: How a version verdict reads in `doctor`, and what to do about it. Every one
#: is a WARN: an out-of-range build is a thing to know before filing a bug, not
#: a reason to refuse to work. Only a build we cannot identify at all is worth
#: more, and even that is a warning, because the launch path does not care.
_VERSION_VERDICTS: dict[str, tuple[str, str]] = {
    "too-old": (
        "below the minimum this build targets",
        "Upgrade QEMU. Older builds are missing devices and command-line "
        "options used here, and the errors do not say so.",
    ),
    "untested": (
        "newer than the highest tested version",
        "Probably fine. Mention it first in any bug report, and bump "
        "KURUKURU_QEMU_VERSION_MAX_TESTED once it has been through the suite.",
    ),
    "prerelease": (
        "a development build, not a release",
        "Fine for local work. Pin a released version for anything shared — "
        "nobody else can install this exact build.",
    ),
    "unknown": (
        "could not be identified",
        "Everything below is unverified. Check KURUKURU_QEMU_SYSTEM_BINARY points "
        "at a real qemu-system-x86_64.",
    ),
}


def _support_checks(support: object) -> list[Check]:
    """Version-range verdict and capability findings, as separate lines.

    Separate on purpose. "QEMU 10.0.94" passing tells you the binary runs;
    it tells you nothing about whether this build can give a guest a TPM, and
    that is the question that decides whether Windows 11 will install. A single
    green QEMU line covering both would be the most reassuring possible way to
    be wrong.
    """
    if not isinstance(support, dict):
        return []

    checks: list[Check] = []
    status = str(support.get("status") or "unknown")
    if status != "ok":
        summary, remedy = _VERSION_VERDICTS.get(
            status, (status, "Check the configured version range.")
        )
        raw = support.get("raw") or support.get("version") or "unknown build"
        checks.append(Check("QEMU version", WARN, f"{raw} — {summary}", remedy))

    for capability in support.get("capabilities") or []:
        if not isinstance(capability, dict) or capability.get("available"):
            continue
        checks.append(
            Check(
                str(capability.get("label") or capability.get("key") or "Capability"),
                WARN,
                str(capability.get("detail") or "unavailable"),
                str(capability.get("consequence") or ""),
            )
        )
    return checks


def _backend_checks(client: ApiClient) -> list[Check]:
    """Everything only the backend's host can answer."""
    try:
        data = client.diagnostics()
    except CliError as exc:
        return [
            Check(
                "Diagnostics",
                FAIL,
                exc.message,
                "The backend answered /health but not /diagnostics — it is "
                "probably an older build. Reinstall it from this checkout.",
            )
        ]

    checks: list[Check] = []
    engine = data.get("engine") or {}

    if engine.get("available") and engine.get("version"):
        checks.append(Check("QEMU", PASS, str(engine["version"])))
        checks.extend(_support_checks(engine.get("support")))
    else:
        checks.append(
            Check(
                "QEMU",
                FAIL,
                str(engine.get("error") or "qemu-system-x86_64 could not be run"),
                "Install QEMU and make sure qemu-system-x86_64 and qemu-img are on "
                "the backend's PATH, then restart it. On Windows the installer does "
                "not add them: set KURUKURU_QEMU_SYSTEM_BINARY and KURUKURU_QEMU_IMG_BINARY "
                "to their full paths instead.",
            )
        )

    if engine.get("accel_available"):
        checks.append(Check("Accelerator", PASS, str(engine.get("accel"))))
    else:
        checks.append(
            Check(
                "Accelerator",
                WARN,
                f"hardware acceleration unavailable (using {engine.get('accel') or 'tcg'})",
                "VMs still run, roughly 30x slower. On Windows, enable 'Windows "
                "Hypervisor Platform' in Windows Features and reboot; on Linux, "
                "make sure /dev/kvm exists and you can read it.",
            )
        )

    if engine.get("base_image_present"):
        checks.append(Check("Base image", PASS, str(engine.get("base_image"))))
    else:
        checks.append(
            Check(
                "Base image",
                WARN,
                "the Ubuntu cloud image has not been downloaded yet",
                f"Nothing to do — it downloads once (~600 MB) on the first "
                f"'{CLI_NAME} launch'. Only a problem if this host has no internet.",
            )
        )

    store = data.get("instance_store") or {}
    free_gb = (store.get("free_bytes") or 0) / 1024**3
    if not store.get("writable"):
        checks.append(
            Check(
                "Instance store",
                FAIL,
                f"{store.get('path')} is not writable by the backend",
                "Create the directory and give the account running the backend "
                "write access, or point KURUKURU_QEMU_DIR somewhere it has.",
            )
        )
    elif free_gb < _LOW_DISK_GB:
        checks.append(
            Check(
                "Instance store",
                WARN,
                f"{store.get('path')} — only {format_bytes(store.get('free_bytes'))} free",
                "Each instance is a sparse overlay that grows towards its disk "
                "size. Free some space or move KURUKURU_QEMU_DIR to a larger volume.",
            )
        )
    else:
        checks.append(
            Check(
                "Instance store",
                PASS,
                f"{store.get('path')} — {format_bytes(store.get('free_bytes'))} free",
            )
        )

    key = data.get("ssh_key") or {}
    if key.get("present"):
        checks.append(Check("SSH keypair", PASS, str(key.get("private_key_path"))))
    else:
        checks.append(
            Check(
                "SSH keypair",
                FAIL,
                str(key.get("error") or "the orchestrator has no keypair"),
                "The backend generates one with ssh-keygen, which must be on its "
                "PATH. On Windows, install the 'OpenSSH Client' optional feature. "
                "Without a key, new instances cannot be logged into.",
            )
        )

    api = data.get("api") or {}
    checks.append(
        Check(
            "Backend",
            PASS,
            f"v{api.get('version', '?')} on Python {data.get('python', '?')}",
        )
    )
    return checks


def _render_doctor(out: Output, checks: list[Check]) -> None:
    if out.json_mode:
        out.emit([asdict(check) for check in checks])
        return

    for check in checks:
        style = _CHECK_STYLES[check.status]
        out.human(f"[{style}]{check.status}[/] {check.name}: {escape(check.detail)}")
        if check.remedy:
            out.human(f"     [dim]{escape(check.remedy)}[/dim]")


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #
def version(
    ctx: typer.Context,
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Print the CLI, API and QEMU versions.

    The one command that does not fail when the API is unreachable: "what
    version is this?" is a question people ask precisely when things are broken,
    and the API's absence is reported as a null rather than an exit code.
    """
    out = Output(json_mode=json_out)
    payload: dict[str, object] = {"cli": cli_version(), "api": None, "qemu": None}

    try:
        data = client_of(ctx).diagnostics()
        payload["api"] = (data.get("api") or {}).get("version")
        payload["qemu"] = (data.get("engine") or {}).get("version")
    except CliError as exc:
        out.warn(exc.message)

    if json_out:
        out.emit(payload)
        return
    out.human(f"{CLI_NAME}  {payload['cli']}")
    out.human(f"api   {payload['api'] or 'unreachable'}")
    out.human(f"qemu  {payload['qemu'] or 'unknown'}")


# --------------------------------------------------------------------------- #
# completion
# --------------------------------------------------------------------------- #
def completion(
    shell: Annotated[
        str,
        typer.Argument(help="bash, zsh, fish, powershell or pwsh."),
    ],
) -> None:
    """Print a shell completion script.

    Printed rather than installed, so the user decides where it goes:

        {cli} completion bash >> ~/.bashrc
        {cli} completion zsh  > ~/.zfunc/_{cli}
        {cli} completion powershell >> $PROFILE
    """
    from typer._completion_classes import completion_init
    from typer._completion_shared import Shells, get_completion_script

    known = [member.value for member in Shells]
    if shell not in known:
        raise CliError(
            f"Unknown shell '{shell}'. Known: {', '.join(known)}.",
            ExitCode.USAGE,
        )

    completion_init()
    Output().human(
        get_completion_script(
            prog_name=CLI_NAME,
            complete_var=f"_{CLI_NAME.replace('-', '_').upper()}_COMPLETE",
            shell=shell,
        ),
        highlight=False,
        markup=False,
    )


completion.__doc__ = (completion.__doc__ or "").format(cli=CLI_NAME)
