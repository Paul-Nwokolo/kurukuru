"""
Disk image registry: probing, importing, and locating image files.

Images live alongside the downloaded base image in ``<qemu_dir>/base-images/``.
Importing copies the user's file in rather than referencing it in place: a
backing file that disappears (moved, unmounted, deleted) silently breaks every
overlay built on it, and a qcow2 overlay cannot be repaired once its backing
file is gone. Owning the bytes is worth the disk space.

The copy runs in a background job because a multi-GB file takes minutes, and the
import request must not block a worker for that long.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.engines.images import base_images_dir
from app.models import Image, ImageFormat

logger = logging.getLogger("kurukuru.images")

#: Streaming chunk size, matching the base-image downloader.
_CHUNK_BYTES = 1024 * 256

#: Formats we accept as a backing file. qemu-img reads more, but these are the
#: ones worth advertising — and refusing the rest keeps us from registering, say,
#: an ISO as a disk image.
_SUPPORTED_FORMATS = {f.value for f in ImageFormat}


class ImageError(Exception):
    """An image file is missing, unreadable, or not a usable disk image."""


@dataclass(frozen=True)
class ProbeResult:
    """What ``qemu-img info`` says about a file."""

    format: ImageFormat
    virtual_size_bytes: int
    actual_size_bytes: int


def image_path(image: Image, settings: Settings | None = None) -> Path:
    """Absolute path to an image's file."""
    return base_images_dir(settings) / image.filename


def probe_image(path: Path, settings: Settings | None = None) -> ProbeResult:
    """Read format and sizes with ``qemu-img info``.

    This is also the validity gate: if qemu-img cannot parse the file, nothing
    downstream can boot it, so the import is rejected here with qemu-img's own
    stderr rather than failing later as a mysterious launch error.
    """
    settings = settings or get_settings()
    cmd = [settings.qemu_img_binary, "info", "--output=json", str(path)]
    logger.debug("probing image: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=settings.cli_timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ImageError(
            f"qemu-img binary '{settings.qemu_img_binary}' not found — is QEMU on PATH?"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ImageError(
            f"'qemu-img info' timed out after {settings.cli_timeout_seconds}s"
        ) from exc

    if proc.returncode != 0:
        raise ImageError(
            f"qemu-img could not read this file: {(proc.stderr or '').strip()}"
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ImageError(f"Could not parse qemu-img output: {exc}") from exc

    raw_format = str(payload.get("format", "")).lower()
    if raw_format not in _SUPPORTED_FORMATS:
        raise ImageError(
            f"Unsupported image format '{raw_format}' "
            f"(supported: {', '.join(sorted(_SUPPORTED_FORMATS))})"
        )

    return ProbeResult(
        format=ImageFormat(raw_format),
        virtual_size_bytes=int(payload.get("virtual-size") or 0),
        actual_size_bytes=int(payload.get("actual-size") or 0),
    )


def resolve_source_file(raw_path: str) -> Path:
    """Validate a user-supplied source path before anything touches it."""
    try:
        path = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ImageError(f"Invalid path '{raw_path}': {exc}") from exc
    if not path.exists():
        raise ImageError(
            f"No file at {path} — the path must be readable by the backend process"
        )
    if not path.is_file():
        raise ImageError(f"{path} is not a file")
    return path


def allocate_filename(name: str, source: Path) -> str:
    """Pick a collision-proof filename for an imported image.

    A uuid prefix rather than the display name: names are user-supplied and
    mutable, and two imports called "ubuntu" must not fight over one file.
    """
    suffix = source.suffix or ".img"
    return f"imported-{uuid.uuid4().hex[:12]}{suffix}"


def copy_into_store(source: Path, filename: str, settings: Settings | None = None) -> Path:
    """Copy an image into the store, leaving no partial file behind on failure."""
    target_dir = base_images_dir(settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    partial = target.with_suffix(target.suffix + ".part")

    try:
        shutil.copyfile(source, partial)
        partial.replace(target)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ImageError(f"Could not copy {source} into the image store: {exc}") from exc

    logger.info("Imported %s -> %s (%d bytes)", source, target, target.stat().st_size)
    return target


def delete_image_file(image: Image, settings: Settings | None = None) -> None:
    """Remove an image's file. Missing file is fine — the row is what matters."""
    try:
        image_path(image, settings).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best effort
        logger.warning("Could not delete image file for '%s': %s", image.name, exc)


def fetch_into_store(
    url: str,
    filename: str,
    *,
    expected_sha256: str | None = None,
    settings: Settings | None = None,
) -> Path:
    """Download an image from a URL into the store, verifying it.

    This is the import route that works the same for every customer, because
    nothing about it touches their filesystem. It is also the one place a
    caller can make the backend consume unbounded disk, so two guards are not
    optional:

    **A size ceiling.** Enforced against the declared ``Content-Length`` *and*
    against the bytes actually received — a server can lie about the former, or
    omit it entirely with chunked encoding.

    **An optional checksum.** Every image provider publishes a SHA256 next to
    the image. Verified over the stream as it is written, so a corrupted or
    substituted download is rejected before it can be launched rather than
    after a guest fails to boot.

    On any failure the partial file is removed: a half-downloaded qcow2 left in
    the store is a trap for whatever tries to probe it next.
    """
    settings = settings or get_settings()
    target_dir = base_images_dir(settings)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / filename
    partial = target.with_suffix(target.suffix + ".part")
    limit = settings.image_fetch_max_bytes

    digest = hashlib.sha256()
    received = 0
    try:
        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=httpx.Timeout(settings.image_fetch_timeout_seconds, connect=30.0),
        ) as response:
            response.raise_for_status()

            declared = int(response.headers.get("content-length") or 0)
            if declared and declared > limit:
                raise ImageError(
                    f"That image is {declared / 1024**3:.1f} GB, over the "
                    f"{limit / 1024**3:.0f} GB import limit "
                    f"(KURUKURU_IMAGE_FETCH_MAX_BYTES)."
                )

            with partial.open("wb") as fh:
                for chunk in response.iter_bytes(_CHUNK_BYTES):
                    received += len(chunk)
                    if received > limit:
                        # The declared length was absent or wrong; stop paying
                        # for someone else's mistake mid-stream.
                        raise ImageError(
                            f"Download exceeded the {limit / 1024**3:.0f} GB import "
                            f"limit (KURUKURU_IMAGE_FETCH_MAX_BYTES)."
                        )
                    digest.update(chunk)
                    fh.write(chunk)
    except httpx.HTTPStatusError as exc:
        partial.unlink(missing_ok=True)
        raise ImageError(
            f"{url} returned HTTP {exc.response.status_code}. Check the URL is a "
            f"direct link to an image file, not a download page."
        ) from exc
    except httpx.HTTPError as exc:
        partial.unlink(missing_ok=True)
        raise ImageError(f"Could not download {url}: {exc}") from exc
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise ImageError(f"Could not write {partial}: {exc}") from exc
    except ImageError:
        partial.unlink(missing_ok=True)
        raise

    if expected_sha256:
        actual = digest.hexdigest()
        if actual != expected_sha256:
            partial.unlink(missing_ok=True)
            raise ImageError(
                f"Checksum mismatch — the downloaded file is not the one the "
                f"checksum describes.\nexpected {expected_sha256}\ngot      {actual}"
            )

    partial.replace(target)
    logger.info("Fetched %s -> %s (%d bytes)", url, target, received)
    return target
