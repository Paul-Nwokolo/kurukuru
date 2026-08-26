"""Lock a file down to its owning OS user, on both platforms, and say so honestly.

``os.chmod`` is the obvious tool and it is the wrong one on Windows: measured on
NTFS, ``chmod(0o600)`` leaves the DACL **byte-identical** and does not even set
the read-only attribute. Access control on Windows is the DACL, and the DACL is
reached through ``icacls`` (which ships with the OS) or pywin32 (which does not,
and would be a dependency for something a subprocess already does).

What a hardened file actually buys, measured rather than assumed:

* **Other OS users cannot read it**, including other non-elevated
  administrators — an ``Administrators`` ACE does not help a filtered
  (non-elevated) admin token.
* **Same-user processes can.** Anything running as the owner reads it, and even
  if the ACE were removed the owner could re-grant itself, because owners hold
  ``WRITE_DAC`` implicitly. This is exactly the POSIX ``0600`` boundary, not a
  weaker one: same-user processes are inside the trust boundary on both.
* **SYSTEM and elevated administrators can**, by taking ownership — the
  equivalent of ``root``.

And the case that has to fail loudly rather than quietly: **ACLs are an
NTFS/ReFS feature.** Point the state directory at FAT32 or exFAT — a USB stick,
a cheap SD card — and there is no DACL to set. ``icacls`` reports success on
some of these, so "the command worked" is not evidence the file is protected.
:func:`harden_file` checks the filesystem first and refuses to claim a
protection it cannot provide.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("kurukuru.fs")

#: Owner read/write, nothing for group or other. POSIX only.
_FILE_MODE = 0o600
#: Owner rwx. A directory others can list is how a secret gets found.
_DIR_MODE = 0o700

#: Filesystems that carry a DACL. Anything else cannot enforce a permission and
#: must not be told it did.
_ACL_FILESYSTEMS = {"NTFS", "REFS"}


class PermissionHardeningError(RuntimeError):
    """The file could not be protected, and the caller must not pretend it was."""


@dataclass(frozen=True)
class HardenResult:
    """What actually happened, so a caller can report it truthfully."""

    path: Path
    #: True only when the OS enforced a restriction. False means "we asked and
    #: the filesystem cannot do it" — the file is readable by anyone with the
    #: path.
    protected: bool
    #: Free text for logs and the doctor command: the mechanism used, or why
    #: none was available.
    detail: str

    def __bool__(self) -> bool:
        return self.protected


def _windows_filesystem(path: Path) -> str | None:
    """The filesystem name for the volume holding ``path``, uppercased.

    Uses ``GetVolumeInformationW`` rather than shelling out: this runs before a
    secret is written, on every start, and a PowerShell launch to answer it
    would be the slowest thing in the path.
    """
    try:
        drive = os.path.splitdrive(str(path.resolve()))[0]
        if not drive:
            return None
        buf = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(drive + "\\"),
            buf, ctypes.sizeof(buf) // 2,
            None, None, None,
            fs, ctypes.sizeof(fs) // 2,
        )
        return fs.value.upper() if ok else None
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("Could not determine the filesystem for %s: %s", path, exc)
        return None


def _current_user_sid() -> str:
    """The account to grant. Prefer the SID: it is unambiguous where a display
    name may be localised, duplicated across domains, or contain characters
    that need quoting on a command line."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if out.startswith("S-1-"):
            return out
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        logger.debug("Could not resolve the current SID: %s", exc)
    return os.environ.get("USERNAME", "")


def _icacls(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["icacls", *args], capture_output=True, text=True, timeout=60)


def _harden_windows(path: Path) -> HardenResult:
    filesystem = _windows_filesystem(path)
    if filesystem is not None and filesystem not in _ACL_FILESYSTEMS:
        # The important branch. icacls can report success here while the file
        # stays readable by everyone, so the only honest outcome is to say the
        # protection is unavailable and let the caller decide.
        return HardenResult(
            path, False,
            f"{filesystem} has no access control lists, so {path.name} cannot be "
            f"restricted to your account. Move the state directory to an NTFS "
            f"volume (KURUKURU_STATE_DIR), or treat this file as readable by anyone "
            f"with access to the disk.",
        )

    principal = _current_user_sid()
    if not principal:
        return HardenResult(path, False, "could not determine the current user account")

    # Order matters: dropping inheritance first means the grant below is the
    # only ACE on the file, rather than being added to the inherited three.
    stripped = _icacls(str(path), "/inheritance:r")
    granted = _icacls(str(path), "/grant:r", f"*{principal}:(R,W)")
    if granted.returncode != 0:
        return HardenResult(
            path, False,
            f"icacls could not grant access ({granted.stderr.strip() or granted.stdout.strip()})",
        )
    if stripped.returncode != 0:
        # Granted but still inheriting: SYSTEM and Administrators remain. Better
        # than nothing and worse than asked for, so it is reported as such.
        return HardenResult(
            path, False,
            "granted to your account, but inherited entries could not be removed — "
            "administrators on this machine can still read it",
        )
    return HardenResult(path, True, f"restricted to {principal} (NTFS ACL, inheritance removed)")


def _harden_posix(path: Path) -> HardenResult:
    try:
        os.chmod(path, _FILE_MODE)
    except OSError as exc:
        return HardenResult(path, False, f"chmod failed: {exc}")
    mode = path.stat().st_mode & 0o777
    if mode != _FILE_MODE:
        # Some filesystems (FAT on a Linux host, certain network mounts) accept
        # the call and store nothing. Same failure as the Windows branch above,
        # and it deserves the same honesty.
        return HardenResult(
            path, False,
            f"the filesystem did not keep the mode (asked for {_FILE_MODE:o}, "
            f"got {mode:o}); this file is readable by other users",
        )
    return HardenResult(path, True, f"mode {mode:o}")


def harden_file(path: Path, *, strict: bool = False) -> HardenResult:
    """Restrict ``path`` to the current OS user. Returns what actually happened.

    ``strict=True`` raises :class:`PermissionHardeningError` instead of
    returning an unprotected result — for callers that would rather fail than
    write a secret in the open.
    """
    result = (_harden_windows(path) if sys.platform == "win32" else _harden_posix(path))
    if result.protected:
        logger.debug("Hardened %s: %s", path, result.detail)
    else:
        logger.warning("Could NOT protect %s: %s", path, result.detail)
        if strict:
            raise PermissionHardeningError(result.detail)
    return result


def harden_directory(path: Path) -> HardenResult:
    """Same, for a directory. A listable directory is half of a leak."""
    if sys.platform == "win32":
        return _harden_windows(path)
    try:
        os.chmod(path, _DIR_MODE)
    except OSError as exc:
        return HardenResult(path, False, f"chmod failed: {exc}")
    return HardenResult(path, True, f"mode {_DIR_MODE:o}")


def describe_protection(path: Path) -> str:
    """A one-line, human-readable statement of who can read ``path``.

    Used by the doctor command and the first-run output, because "your token is
    at C:\\...\\token" is only reassuring if the next line says who else can
    read it.
    """
    if not path.exists():
        return f"{path} does not exist"
    if sys.platform == "win32":
        fs = _windows_filesystem(path) or "unknown"
        if fs not in _ACL_FILESYSTEMS:
            return f"{path}: on {fs}, which has no ACLs — readable by anyone with the disk"
        out = _icacls(str(path)).stdout
        entries = [
            line.replace(str(path), "").strip()
            for line in out.splitlines()
            if line.strip() and "Successfully processed" not in line
        ]
        return f"{path}: {', '.join(entries) if entries else 'no ACL entries reported'}"
    mode = path.stat().st_mode & 0o777
    return f"{path}: mode {mode:o}"
