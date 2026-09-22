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

from kurukuru.config import Settings, get_settings

logger = logging.getLogger("kurukuru.qemu.images")

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


#: The way out when the download cannot work at all. Named once so the two
#: messages below cannot drift, and phrased as a route through the product
#: rather than as a setting — the user reading this is not looking for
#: KURUKURU_QEMU_BASE_IMAGE_URL, they are looking for a working VM.
_OFFLINE_ROUTE = (
    "If this machine has no internet access, you can use an image you already "
    "have instead: Images -> Add image, then pick it when you launch."
)


def _download_failure_message(url: str, exc: Exception) -> str:
    """Why the base image could not be fetched, led by the likely cause.

    This message is what lands in ``Instance.error_message`` and is the entire
    explanation the user gets for a launch that failed before it started. It
    used to read:

        Base image download failed (https://cloud-images.ubuntu.com/...):
        [WinError 10060] A connection attempt failed because the connected
        party did not properly respond after a period of time...

    Every word of which is true, and none of which says "you are offline" or
    "here is what to do instead". So the plain sentence leads, the route out
    follows, and the raw error is kept underneath it — that last part is not
    politeness, it is what makes a bug report diagnosable.

    The lead is chosen from the exception *type* rather than by matching on
    its text: ``httpx`` already classifies these, and matching on "WinError
    10060" would be matching on one platform's spelling of one cause.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        lead = (
            f"Couldn't download the Ubuntu base image — the server answered "
            f"HTTP {exc.response.status_code}. That is a problem at the other "
            f"end rather than on your machine, and it usually clears by "
            f"itself; try again in a few minutes."
        )
    elif isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        lead = (
            "Couldn't download the Ubuntu base image — check your internet "
            "connection. Kurukuru fetches it once, about 600 MB, the first "
            "time you launch an instance."
        )
    elif isinstance(exc, httpx.TimeoutException):
        lead = (
            "Couldn't download the Ubuntu base image — the connection timed "
            "out part-way through. Check your internet connection and try "
            "again; the download resumes from nothing, so a slow link may "
            "need a few attempts."
        )
    else:
        lead = (
            "Couldn't download the Ubuntu base image — check your internet "
            "connection."
        )
    return f"{lead}\n\n{_OFFLINE_ROUTE}\n\nDetail: {url} — {exc!r}"


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
        raise BaseImageError(_download_failure_message(url, exc)) from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise BaseImageError(f"Could not write base image to {partial}: {exc}") from exc

    if downloaded == 0:
        partial.unlink(missing_ok=True)
        raise BaseImageError(
            f"Couldn't download the Ubuntu base image — the server sent an "
            f"empty file. Try again; if it keeps happening, the mirror is "
            f"broken rather than your connection.\n\n{_OFFLINE_ROUTE}\n\n"
            f"Detail: {url} returned 0 bytes"
        )

    try:
        partial.replace(target)
    except OSError as exc:
        raise BaseImageError(f"Could not finalise base image at {target}: {exc}") from exc

    logger.info("Base image ready: %s (%.1f MiB)", target, downloaded / 1048576)
    return target
