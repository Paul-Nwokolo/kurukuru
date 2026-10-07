"""
``kurukuru service`` and ``kurukuru data``: the Linux install's two host-local jobs.

Both act on this machine directly rather than through the API, and both are
Linux-only. On Windows the installer registers the startup task and the
uninstaller offers data removal (DECISIONS #53, #63); these say so instead of
half-working.

* ``service install | uninstall | status`` — the systemd user unit
  (:mod:`kurukuru.linux_service`). A subcommand rather than the "small shell
  script" the plan first named: it knows the absolute path of the very command
  being run, it is versioned with the code that the unit starts, and it is
  tested like everything else. DECISIONS #71.
* ``data remove`` — measures the state directory, asks, then trashes it or
  renames it aside. Never deletes (:mod:`kurukuru.state_removal`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import typer

from kurukuru import linux_host, linux_service, state_removal
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import format_bytes
from kurukuru.cli.naming import CLI_NAME
from kurukuru.cli.output import Output
from kurukuru.cli.support import client_of
from kurukuru.product import default_config_dir, default_state_dir

service_app = typer.Typer(
    help="Run the backend as a systemd user service (Linux).", no_args_is_help=True
)
data_app = typer.Typer(help="This machine's Kurukuru data (Linux).", no_args_is_help=True)


def _require_linux(what: str) -> None:
    if not sys.platform.startswith("linux"):
        raise CliError(
            f"'{CLI_NAME} {what}' is for Linux installs.",
            ExitCode.USAGE,
            hint="On Windows the installer registers the startup task, and "
            "uninstalling from Settings -> Apps offers to remove your data.",
        )


def _state_dir() -> Path:
    """Resolved the way the backend resolves it: the env override, else policy."""
    return Path(os.environ.get("KURUKURU_STATE_DIR") or default_state_dir()).expanduser()


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #
def _linger_note(out: Output) -> None:
    if linux_host.is_wsl():
        out.warn(
            "This is WSL, which stops the whole distribution when its last "
            "window closes, lingering or not: the service runs while a WSL "
            "session is open and starts again with the distribution."
        )
        return
    linger = linux_host.linger_enabled()
    if linger is True:
        out.note("Lingering is on: the service keeps running after you log out.")
    elif linger is False:
        out.warn(
            "Lingering is off, so systemd stops this service — and with it the "
            "backend — when your last session ends. To keep it running after "
            "logout: sudo loginctl enable-linger $USER"
        )


@service_app.command("install")
def service_install() -> None:
    """Install, enable and (re)start the user service. Safe to re-run after an upgrade."""
    _require_linux("service install")
    out = Output()
    if not linux_host.systemd_user_available():
        raise CliError(
            "No systemd user manager is reachable from this session.",
            ExitCode.FAILURE,
            hint="Common causes: an SSH session on a host that starts no user "
            "manager (enable lingering, then log in again), a container, or "
            "WSL without systemd enabled in /etc/wsl.conf. Without it, run "
            f"'{CLI_NAME} serve' yourself.",
        )
    try:
        executable = linux_service.resolve_executable()
        plan = linux_service.install(executable)
    except linux_service.ServiceError as exc:
        raise CliError(str(exc), ExitCode.FAILURE) from exc
    verb = "unchanged" if plan.unchanged else ("updated" if plan.replaces else "written")
    out.human(f"Unit {verb}: {plan.unit_path}")
    out.human(f"Runs: {executable} serve")
    out.human(f"Enabled and started. Dashboard: http://127.0.0.1:7842")
    _linger_note(out)
    out.note(f"Logs: journalctl --user -u {linux_service.UNIT_NAME} -f")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Stop and remove the user service. Your VMs and data are not touched."""
    _require_linux("service uninstall")
    out = Output()
    try:
        removed = linux_service.uninstall()
    except linux_service.ServiceError as exc:
        raise CliError(str(exc), ExitCode.FAILURE) from exc
    out.human("Service stopped and removed." if removed else "No service was installed.")
    out.note(f"Your data is still in {_state_dir()}. '{CLI_NAME} data remove' removes it, "
             f"if that is what you want.")


@service_app.command("status")
def service_status(
    json_out: Annotated[bool, typer.Option("--json", help="Emit JSON only.")] = False,
) -> None:
    """Whether the service is installed, enabled, running, and will outlive logout."""
    _require_linux("service status")
    out = Output(json_mode=json_out)
    try:
        info = linux_service.status()
    except linux_service.ServiceError as exc:
        raise CliError(str(exc), ExitCode.FAILURE) from exc
    info["linger"] = linux_host.linger_enabled()
    if json_out:
        out.emit(info)
        return
    if not info["installed"]:
        out.human(f"not installed ({info['unit_path']})")
        out.note(f"Install it with '{CLI_NAME} service install'.")
        return
    out.human(f"unit     {info['unit_path']}")
    out.human(f"active   {info['active']}")
    out.human(f"enabled  {info['enabled']}")
    out.human(f"linger   {'on' if info['linger'] else 'off' if info['linger'] is False else 'unknown'}")


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
@data_app.command("remove")
def data_remove(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask. Still never deletes.")] = False,
) -> None:
    """Move this machine's Kurukuru data to the trash, or aside. Never deletes it."""
    _require_linux("data remove")
    out = Output()
    root = _state_dir()
    if not root.exists():
        out.human(f"Nothing to remove: {root} does not exist.")
        return

    # The backend has open disks and a live database in there. Moving the tree
    # from under it is how a VM disk gets corrupted.
    try:
        client_of(ctx).health()
        raise CliError(
            "The backend is running, with this data open.",
            ExitCode.CONFLICT,
            hint=f"Stop it first: systemctl --user stop {linux_service.UNIT_NAME} "
            f"(or stop '{CLI_NAME} serve'), and stop any running VMs.",
        )
    except CliError as exc:
        if exc.code is not ExitCode.UNREACHABLE:
            raise

    rows = state_removal.inventory(root)
    total = sum(size for _, size in rows)
    out.human(f"{root} holds {format_bytes(total)}:")
    for label, size in rows:
        out.human(f"  {label:<20} {format_bytes(size)}")
    config = Path(default_config_dir()).expanduser()
    if config != root and config.exists():
        out.human(f"and configuration in {config}")
    out.human("Database backups live in here too, so this removes them as well.")

    if not yes and not typer.confirm("Move all of this out of Kurukuru's reach?", default=False):
        out.human("Nothing removed.")
        return

    for target in [root] + ([config] if config != root and config.exists() else []):
        try:
            done = state_removal.remove(target)
        except OSError as exc:
            raise CliError(f"Could not move {target}: {exc}", ExitCode.FAILURE) from exc
        if done.destination is not None:
            out.human(f"Moved {target} to {done.destination}")
            out.note(f"No trash is available here, so nothing was deleted. When you "
                     f"are sure: rm -rf '{done.destination}'")
        else:
            out.human(f"Moved {target} to the trash ({done.method}); restore it from there.")
