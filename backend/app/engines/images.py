"""
Base image acquisition for the QEMU engine.

Phase 5 ships exactly one base image: the official Ubuntu 24.04 (noble) cloud
image, a qcow2 that already contains cloud-init and needs no installer. Every
instance disk is a copy-on-write overlay on top of it, so the (~600 MB)
download happens once per host and never again.

The download is streamed to a ``.part`` file and only renamed into place once
complete, so an interrupted download can never be mistaken for a usable image.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger("iaas.qemu.images")

# Log at most this often while streaming, so a 600 MB download produces a
# handful of lines rather than thousands.
_PROGRESS_STEP_BYTES = 32 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024


class BaseImageError(Exception):
    """The base image could not be downloaded or is unusable."""


def base_images_dir(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return Path(settings.qemu_dir).expanduser() / "base-images"


def base_image_path(settings: Settings | None = None) -> Path:
    """Absolute path the base image occupies once downloaded (may not exist)."""
    settings = settings or get_settings()
    return base_images_dir(settings) / settings.qemu_base_image_name


def ensure_base_image(settings: Settings | None = None) -> Path:
    """Return the local base image path, downloading it first if absent.

    Idempotent and safe to call on every provision: the common case is a single
    ``exists()`` check.
    """
    settings = settings or get_settings()
    target = base_image_path(settings)
    if target.exists() and target.stat().st_size > 0:
        logger.debug("Base image present: %s (%d bytes)", target, target.stat().st_size)
        return target

    url = settings.qemu_base_image_url
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")

    logger.info("Downloading base image %s -> %s", url, target)
    downloaded = 0
    next_log = _PROGRESS_STEP_BYTES
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(settings.qemu_download_timeout_seconds, connect=30.0),
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            with partial.open("wb") as fh:
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if downloaded >= next_log:
                        pct = f" ({downloaded * 100 // total}%)" if total else ""
                        logger.info(
                            "Base image download: %.1f MiB%s",
                            downloaded / 1048576,
                            pct,
                        )
                        next_log += _PROGRESS_STEP_BYTES
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise BaseImageError(f"Base image download failed ({url}): {exc}") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BaseImageError(f"Could not write base image to {partial}: {exc}") from exc

    if downloaded == 0:
        partial.unlink(missing_ok=True)
        raise BaseImageError(f"Base image download from {url} produced an empty file")

    try:
        partial.replace(target)
    except OSError as exc:
        raise BaseImageError(f"Could not finalise base image at {target}: {exc}") from exc

    logger.info("Base image ready: %s (%.1f MiB)", target, downloaded / 1048576)
    return target
