"""
NoCloud seed ISO generation.

QEMU has no flag for handing cloud-init a config file, so it is delivered the
way cloud-init's *NoCloud* datasource expects: a tiny read-only volume, attached
as a CD-ROM, whose **volume label is CIDATA** and which contains these files at
the root:

    user-data       the #cloud-config document
    meta-data       instance-id + local-hostname
    network-config  optional netplan-style config (see QemuEngine)

The label is the whole discovery mechanism — cloud-init scans block devices for
a filesystem labelled ``cidata``/``CIDATA``. Get it wrong and the guest boots
with no user, no key, and no way in.

The ISO is built with :mod:`pycdlib` in pure Python rather than shelling out to
``genisoimage``/``mkisofs``, which do not exist on a stock Windows host.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pycdlib

logger = logging.getLogger("kurukuru.qemu.seed")

# cloud-init matches the label case-insensitively; ISO-9660 itself only allows
# uppercase in a volume identifier, so CIDATA is the canonical spelling.
VOLUME_LABEL = "CIDATA"

# ISO-9660 level 1 caps names at 8.3 + version, hence USER_DATA / META_DATA in
# the primary volume descriptor. cloud-init reads the Joliet names, where the
# real hyphenated filenames live.
_FILES = {
    "user-data": ("/USERDATA.;1", "/user-data"),
    "meta-data": ("/METADATA.;1", "/meta-data"),
    "network-config": ("/NETWORKC.;1", "/network-config"),
}


class SeedIsoError(Exception):
    """The NoCloud seed ISO could not be written."""


def build_meta_data(instance_name: str) -> str:
    """Return the ``meta-data`` document for ``instance_name``.

    ``instance-id`` is what cloud-init uses to decide whether this is a *first*
    boot: keeping it stable per instance means a restart re-runs only the
    per-boot modules, not the whole user-creation dance.
    """
    return f"instance-id: {instance_name}\nlocal-hostname: {instance_name}\n"


def build_seed_iso(
    path: Path,
    *,
    user_data: str,
    meta_data: str,
    network_config: str | None = None,
) -> Path:
    """Write a NoCloud seed ISO at ``path`` and return it.

    ``network_config`` is omitted from the volume entirely when None, which is
    what makes cloud-init fall back to its own DHCP defaults.

    Overwrites any existing file so re-provisioning is idempotent.
    """
    payloads = {
        "user-data": user_data.encode("utf-8"),
        "meta-data": meta_data.encode("utf-8"),
    }
    if network_config is not None:
        payloads["network-config"] = network_config.encode("utf-8")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        iso = pycdlib.PyCdlib()
        # joliet=3 gives the long, case-preserving names cloud-init looks for.
        iso.new(interchange_level=1, joliet=3, vol_ident=VOLUME_LABEL)
        for name, blob in payloads.items():
            iso_path, joliet_path = _FILES[name]
            iso.add_fp(io.BytesIO(blob), len(blob), iso_path, joliet_path=joliet_path)
        iso.write(str(path))
        iso.close()
    except (OSError, pycdlib.pycdlibexception.PyCdlibException) as exc:
        raise SeedIsoError(f"Could not build NoCloud seed ISO at {path}: {exc}") from exc

    logger.info("Built NoCloud seed ISO %s (%d bytes)", path, path.stat().st_size)
    return path


def read_seed_file(path: Path, name: str) -> str:
    """Read one file back out of a seed ISO (used by tests and diagnostics)."""
    if name not in _FILES:
        raise SeedIsoError(f"Unknown seed file '{name}'")
    _, joliet_path = _FILES[name]
    buffer = io.BytesIO()
    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(path))
        iso.get_file_from_iso_fp(buffer, joliet_path=joliet_path)
    except (OSError, pycdlib.pycdlibexception.PyCdlibException) as exc:
        raise SeedIsoError(f"Could not read '{name}' from {path}: {exc}") from exc
    finally:
        iso.close()
    return buffer.getvalue().decode("utf-8")
