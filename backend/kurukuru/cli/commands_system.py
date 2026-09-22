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
    app.command("capacity")(capacity)
    app.command("doctor")(doctor)
    app.command("version")(version)
    app.command("completion")(completion)


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


def _application_control_checks(data: dict, *, engine_ok: bool) -> list[Check]:
    """Whether Windows will let this install run its own bundled programs.

    The check that did not exist when it was needed. On a fresh Windows 11
    machine Smart App Control is on by default and refuses anything it does
    not recognise; Kurukuru is not code-signed, so it refuses Kurukuru. What
    the user saw was an engine that reported unavailable with a working QEMU
    sitting beside it, and a status code in the billions.

    The verdict depends on whether the engine actually got blocked, because
    the same state means two different things. Enforcing *and* the engine is
    down is the diagnosis. Enforcing while everything works means this install
    has been allowed through — worth knowing before the next upgrade replaces
    the files it was allowed on, but not a fault today.
    """
    sac = data.get("smart_app_control") or {}
    state = str(sac.get("state") or "unknown")
    detail = str(sac.get("detail") or "")

    if state in ("unsupported", "off"):
        # Nothing to say when the feature is absent or off — except when
        # something clearly blocked the engine anyway, in which case the
        # useful fact is that it was *not* Smart App Control.
        if state == "off" and not engine_ok:
            return [
                Check(
                    "Application control",
                    PASS,
                    "Smart App Control is off",
                    "So it is not what stopped QEMU. If QEMU failed with a "
                    "0xC0E90002 status, this machine has some other "
                    "application-control policy — on a work machine that is "
                    "managed by your IT administrator, who has to allow it.",
                )
            ]
        return []

    if state == "unknown":
        return [
            Check(
                "Application control",
                WARN,
                detail or "Smart App Control's state could not be read",
                "Not a fault in itself. It matters only if Kurukuru or QEMU "
                "will not start, which this cannot now rule in or out.",
            )
        ]

    if state == "enforced":
        return [
            Check(
                "Application control",
                FAIL if not engine_ok else WARN,
                detail,
                (
                    "This is why QEMU will not run. Kurukuru's build is not "
                    "code-signed, so Smart App Control refuses to load it — "
                    "nothing is wrong with your install or your QEMU. To use "
                    "Kurukuru, turn Smart App Control off in Windows Security "
                    "-> App & browser control -> Smart App Control. That is a "
                    "machine-wide security setting protecting everything else "
                    "you run, so weigh it rather than just clicking through."
                    if not engine_ok
                    else "Kurukuru is running, so this install has been let "
                    "through. Worth knowing before an upgrade: new files have "
                    "to earn that again, and a future release may be blocked "
                    "where this one was not."
                ),
            )
        ]

    # Evaluation mode: blocking nothing yet, may start.
    return [
        Check(
            "Application control",
            WARN,
            detail,
            "Nothing to do today. If Kurukuru stops working after a Windows "
            "update, this is the first thing to re-check.",
        )
    ]


def _override_checks(data: dict) -> list[Check]:
    """Whether someone has pointed this install at a QEMU by hand.

    Kurukuru bundles QEMU and resolves it beside its own executable, so an
    override is almost always a leftover. The 0.1.0 release note told people
    to set these two variables to work around the bug where nothing pointed at
    the bundle — advice that was right for 0.1.0 and has been actively harmful
    since 0.1.1, which resolves the bundle correctly on its own. Left in place
    it replaces a working answer with a hardcoded path that an upgrade, a move
    or a different install shape can invalidate.
    """
    overrides = data.get("qemu_overrides") or {}
    if not isinstance(overrides, dict) or not overrides:
        return []

    checks: list[Check] = []
    for env, info in overrides.items():
        if not isinstance(info, dict):
            continue
        in_use = str(info.get("in_use") or "?")
        would_be = str(info.get("would_be") or "?")
        exists = bool(info.get("exists"))
        checks.append(
            Check(
                "QEMU override",
                FAIL if not exists else WARN,
                f"{env} points at {in_use}"
                + ("" if exists else " — and there is no file there"),
                f"Kurukuru would otherwise use {would_be}. If you set this to "
                f"work around the 0.1.0 bundled-QEMU bug, that workaround is "
                f"for 0.1.0 only and should be removed: this build finds its "
                f"own QEMU. Clear it with 'setx {env} \"\"' and sign out and "
                f"back in, so the startup task stops inheriting it.",
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
    engine_ok = bool(engine.get("available") and engine.get("version"))

    # Before QEMU, because it explains QEMU. An enforcing Smart App Control is
    # the reason the engine is unavailable, not a separate finding, and a
    # reader who meets "install QEMU" first will act on that instead.
    checks.extend(_application_control_checks(data, engine_ok=engine_ok))

    if engine_ok:
        checks.append(Check("QEMU", PASS, str(engine["version"])))
        checks.extend(_support_checks(engine.get("support")))
    else:
        checks.append(
            Check(
                "QEMU",
                FAIL,
                str(engine.get("error") or "qemu-system-x86_64 could not be run"),
                # The installed build bundles QEMU and finds it for itself, so
                # "install QEMU" is advice for a checkout, not for the audience
                # most likely to be reading this. Both are named, in the order
                # that matches who hits it.
                "On an installed build QEMU ships with Kurukuru and is found "
                "automatically — if this fails there, the check above is the "
                "usual reason, and reinstalling is the fix for a damaged "
                "bundle. Running from a checkout, install QEMU and put "
                "qemu-system-x86_64 and qemu-img on the backend's PATH.",
            )
        )

    checks.extend(_override_checks(data))

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
