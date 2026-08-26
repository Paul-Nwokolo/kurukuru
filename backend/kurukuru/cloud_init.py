"""
Per-instance cloud-init generation.

Renders a ``#cloud-config`` YAML file that provisions each VM with a default
sudo user, the orchestrator's SSH public key, a package-index refresh and a
couple of baseline packages. The file is written per-instance, handed to
``multipass launch --cloud-init``, and deleted once launch completes.

The YAML is built as a Python dict and serialized with ``yaml.safe_dump`` —
never string-templated — so special characters in keys or names can't corrupt
the document.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from kurukuru.config import Settings, get_settings
from kurukuru.ssh_keys import SSHKeyError, get_public_key
from kurukuru.user_data import UserDataError, render_merged

logger = logging.getLogger("kurukuru.cloudinit")

# Baseline packages installed on first boot.
_BASELINE_PACKAGES = ["curl", "htop"]


class CloudInitError(Exception):
    """Raised when the cloud-init document cannot be built or written."""


def _cloud_init_dir(settings: Settings) -> Path:
    return Path(settings.cloud_init_dir).expanduser()


def build_config(
    instance_name: str,
    settings: Settings | None = None,
    public_keys: list[str] | None = None,
) -> dict:
    """Build the cloud-config payload as a dict (no I/O).

    Separated from :func:`build_cloud_init` so tests can assert on structure
    without touching the filesystem.

    ``public_keys`` is the set of keys to install. Omitted means the
    orchestrator's alone, which is what every instance got before key pairs
    existed — callers that don't care keep the old behaviour exactly. An empty
    list is honoured as "no keys at all": the caller has said something
    definite, and inventing a key would make the instance reachable by someone
    who asked for it not to be.
    """
    settings = settings or get_settings()
    if public_keys is None:
        try:
            public_keys = [get_public_key(settings)]
        except SSHKeyError as exc:
            # Propagate as CloudInitError so the caller has one failure type.
            raise CloudInitError(
                f"Cannot build cloud-init for '{instance_name}': {exc}"
            ) from exc

    return {
        "users": [
            {
                "name": settings.default_vm_user,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "shell": "/bin/bash",
                "ssh_authorized_keys": list(public_keys),
            }
        ],
        "package_update": True,
        "packages": list(_BASELINE_PACKAGES),
    }


def render_user_data(
    instance_name: str,
    settings: Settings | None = None,
    public_keys: list[str] | None = None,
    custom_user_data: str | None = None,
) -> str:
    """Return the complete ``#cloud-config`` document as text (no I/O).

    This is the single rendering of the cloud-config: :func:`build_cloud_init`
    writes it to disk for the provisioning job, and the QEMU engine embeds the
    same bytes as the ``user-data`` file of a NoCloud seed ISO. One renderer
    means two callers can never provision differently.
    """
    settings = settings or get_settings()
    config = build_config(instance_name, settings, public_keys=public_keys)
    # The user's cloud-config is merged onto ours structurally, never appended
    # as text — see app.user_data. cloud-init requires the "#cloud-config"
    # header on the first line, and width is set high in there so long values
    # (an SSH key) never wrap mid-line.
    try:
        return render_merged(config, custom_user_data)
    except UserDataError as exc:
        raise CloudInitError(str(exc)) from exc


def build_cloud_init(
    instance_name: str,
    settings: Settings | None = None,
    public_keys: list[str] | None = None,
    custom_user_data: str | None = None,
) -> Path:
    """Render the cloud-config for ``instance_name`` to a YAML file and return its path.

    The file is named ``<instance-name>.yaml`` under ``settings.cloud_init_dir``.
    """
    settings = settings or get_settings()
    document = render_user_data(
        instance_name, settings, public_keys=public_keys, custom_user_data=custom_user_data
    )

    target_dir = _cloud_init_dir(settings)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{instance_name}.yaml"
        # newline="\n" explicitly: this file is a *guest* artifact, and text
        # mode would translate every line ending to CRLF on Windows. It happens
        # to survive today because the engine reads it back through universal
        # newlines before embedding it in the seed ISO — that is, the bug is
        # cancelled by an accident of the read path, and the obvious
        # optimisation (read_bytes, skip the re-decode) would uncancel it and
        # hand cloud-init a CRLF document. Write what we mean instead.
        path.write_text(document, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise CloudInitError(
            f"Could not write cloud-init file for '{instance_name}': {exc}"
        ) from exc

    logger.info("Wrote cloud-init for '%s' -> %s", instance_name, path)
    return path


def cleanup(path: Path | str | None) -> None:
    """Delete a rendered cloud-init file. Safe to call with None or a missing file."""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
        logger.debug("Removed cloud-init file %s", path)
    except OSError as exc:  # pragma: no cover - best effort
        logger.warning("Could not remove cloud-init file %s: %s", path, exc)
