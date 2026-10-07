"""
The systemd *user* service that keeps the backend running on Linux.

A user service, not a system one, for the reasons the Windows build uses a
logon task rather than a Windows service (DECISIONS #53, #71): no root to install
it, it runs as the user who owns the state directory so file permissions stay
what ``fs_permissions`` set them to, and there is no second account whose home
holds the VMs.

Two behaviours are stated here because the defaults would get them wrong:

* **VMs survive a service restart.** QEMU is spawned in its own session and
  tracked through the filesystem, so restarting the backend — an upgrade, a
  crash, ``systemctl --user restart`` — is meant to leave running VMs alone.
  systemd's default ``KillMode=control-group`` would kill every one of them,
  since they live in the service's cgroup. ``KillMode=process`` signals only
  the backend. (systemd prints a warning about leftover processes at the next
  start; that is the VMs, and it is the intended state.)
* **The service stops at logout unless lingering is on.** Without
  ``loginctl enable-linger``, systemd stops the user manager — and this unit —
  when the user's last session ends. The install reports which applies rather
  than implying the service always runs.

Only ``systemctl`` is called, always as an argument list with a timeout, and
everything that decides *what* to do is pure so it is tested on any OS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

UNIT_NAME = "kurukuru.service"
SYSTEMCTL_TIMEOUT_SECONDS = 30
DOCS_URL = "https://github.com/Paul-Nwokolo/kurukuru/blob/main/docs/INSTALL-LINUX.md"


class ServiceError(RuntimeError):
    """A systemctl step failed; the message carries systemctl's own words."""


def unit_dir(environ=None) -> Path:
    """``$XDG_CONFIG_HOME/systemd/user``, where systemd looks for user units."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME") or ""
    # Path.is_absolute, not startswith("/"): the string test treated a Windows
    # temp path as relative under test, fell back to the real home, and wrote a
    # unit file into the developer's ~/.config. Same meaning on Linux.
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "systemd" / "user"


def render_unit(executable: str) -> str:
    """The unit file. ``executable`` must be absolute: a user manager's PATH is
    not the login shell's, and ``~/.local/bin`` — where pipx puts the command —
    is usually not on it."""
    if not executable.startswith("/"):
        raise ServiceError(f"The service needs an absolute path to kurukuru, not {executable!r}.")
    return f"""\
# Installed by `kurukuru service install`. Re-run that command after moving or
# reinstalling Kurukuru; `kurukuru service uninstall` removes this file.
[Unit]
Description=Kurukuru local cloud: API and dashboard on 127.0.0.1:7842
Documentation={DOCS_URL}

[Service]
Type=exec
ExecStart={executable} serve
Restart=on-failure
RestartSec=5
# Running VMs are separate QEMU processes the backend re-adopts on start, so a
# restart or an upgrade must not take them down with it. The default
# (control-group) would kill every VM in this unit's cgroup.
KillMode=process

[Install]
WantedBy=default.target
"""


def resolve_executable(argv0: str | None = None) -> str:
    """The absolute path of the ``kurukuru`` command running now.

    The one being run is the one to install, rather than whichever ``kurukuru``
    PATH happens to find first — a source checkout's venv and a pipx install
    side by side would otherwise install the wrong one silently.
    """
    import sys

    candidate = argv0 or sys.argv[0]
    path = Path(candidate)
    if not path.is_absolute():
        found = shutil.which(candidate) or shutil.which("kurukuru")
        if found is None:
            raise ServiceError("Could not find the kurukuru command to install as a service.")
        path = Path(found)
    return str(path.resolve())


@dataclass(frozen=True)
class InstallPlan:
    """What ``install`` will do, decided before anything is touched."""

    unit_path: Path
    content: str
    replaces: bool          # a unit file is already there
    unchanged: bool         # ...and it is byte-identical to this one


def plan_install(executable: str, environ=None) -> InstallPlan:
    path = unit_dir(environ) / UNIT_NAME
    content = render_unit(executable)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    return InstallPlan(path, content, existing is not None, existing == content)


def systemctl(*args: str, runner=subprocess.run) -> str:
    """``systemctl --user ARGS``. Raises ServiceError with systemctl's stderr."""
    command = ["systemctl", "--user", *args]
    try:
        result = runner(command, capture_output=True, text=True,
                        timeout=SYSTEMCTL_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as exc:
        raise ServiceError("systemctl is not installed; this host does not run systemd.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(f"`{' '.join(command)}` did not finish in {SYSTEMCTL_TIMEOUT_SECONDS}s.") from exc
    if result.returncode != 0 and args[0] not in ("is-active", "is-enabled"):
        detail = (result.stderr or result.stdout or "").strip()
        raise ServiceError(f"`{' '.join(command)}` failed: {detail or f'exit {result.returncode}'}")
    return (result.stdout or "").strip()


def install(executable: str, *, environ=None, runner=subprocess.run) -> InstallPlan:
    """Write the unit, reload, enable and start it. Idempotent."""
    plan = plan_install(executable, environ)
    plan.unit_path.parent.mkdir(parents=True, exist_ok=True)
    if not plan.unchanged:
        plan.unit_path.write_text(plan.content, encoding="utf-8", newline="\n")
    systemctl("daemon-reload", runner=runner)
    systemctl("enable", UNIT_NAME, runner=runner)
    # restart, not start: after an upgrade the running backend is the old code.
    systemctl("restart", UNIT_NAME, runner=runner)
    return plan


def uninstall(*, environ=None, runner=subprocess.run) -> bool:
    """Stop, disable and remove the unit. Never touches the state directory.

    Returns whether there was a unit to remove.
    """
    path = unit_dir(environ) / UNIT_NAME
    if not path.exists():
        return False
    systemctl("disable", "--now", UNIT_NAME, runner=runner)
    path.unlink()
    systemctl("daemon-reload", runner=runner)
    return True


def status(*, environ=None, runner=subprocess.run) -> dict[str, object]:
    path = unit_dir(environ) / UNIT_NAME
    if not path.exists():
        return {"installed": False, "unit_path": str(path)}
    return {
        "installed": True,
        "unit_path": str(path),
        "active": systemctl("is-active", UNIT_NAME, runner=runner) or "unknown",
        "enabled": systemctl("is-enabled", UNIT_NAME, runner=runner) or "unknown",
    }
