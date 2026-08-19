"""What the installed QEMU is, and what it can actually do.

Two questions that look like one and are not.

**Which version is installed** is cheap to ask and worth recording. A guest that
worked last month and does not today, on a host somebody upgraded in between, is
otherwise a mystery: nothing in the instance row says which hypervisor built it.
So a known-good range lives in config, the installed build is compared against
it at startup, and the version is stamped onto every instance at launch.

**What the build can do** is the more important half, and a version number
cannot answer it. Every finding that shaped Phase 13 is invisible to a version
comparison:

* TPM emulation is compiled out of *every* Windows build — QEMU's ``meson.build``
  requires ``host_os != 'windows'`` for the feature — so a perfectly current,
  in-range QEMU still cannot give a guest a TPM, and Windows 11 still cannot
  install. No upgrade fixes this; it is not a version problem.
* UEFI firmware under WHPX dies on the ``pflash`` MMIO path, a bug open upstream
  since 2019 and unfixed in every release since. Again in-range, again broken.
* ``-cpu host``/``max`` kill WHPX outright, which is a property of the
  accelerator, not the build.

A check that reported "QEMU 10.0.94 — supported" while the user's Windows 11
install failed at the hardware check would be worse than no check at all. So
capabilities are *probed*, and the probes are what `doctor`, `/health` and
Settings actually show.

Everything here is advisory. Nothing refuses a launch: a warning that turns out
to be wrong costs a sentence, and a gate that turns out to be wrong costs the
user their VM.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.config import Settings

logger = logging.getLogger("iaas.qemu.capabilities")

#: `QEMU emulator version 10.0.94 (v10.1.0-rc4-12093-gbd0a254583)`
#: The bare triple is what compares; the parenthetical is what tells you the
#: build is not a release.
_VERSION = re.compile(
    r"version\s+(?P<version>\d+(?:\.\d+){0,2})\s*(?:\((?P<detail>[^)]*)\))?"
)

#: Markers that mean "this is not a released build". A release reads
#: `version 9.2.0`; anything carrying an rc tag or a git describe suffix is a
#: snapshot of somebody's tree.
_PRERELEASE = re.compile(r"-rc\d+|-g[0-9a-f]{7,}|dirty|alpha|beta", re.IGNORECASE)


@dataclass(frozen=True)
class QemuVersion:
    """A parsed ``qemu-system-x86_64 --version``."""

    raw: str
    release: tuple[int, ...]
    #: The parenthetical build string, when there is one.
    build: str | None = None

    @property
    def text(self) -> str:
        return ".".join(str(part) for part in self.release)

    @property
    def is_prerelease(self) -> bool:
        """Whether this is a development snapshot rather than a release.

        Its own state, not a version comparison, because it cannot be one: this
        host runs 10.0.94 from ``v10.1.0-rc4-12093-g…``, a tree 12k commits past
        an rc. Numerically that is *below* 10.1.0 and above 10.0.0, and neither
        comparison says the useful thing — which is that nobody else can install
        this exact build, so "tested against it" is a claim about one machine.
        """
        return bool(self.build and _PRERELEASE.search(self.build))


def parse_version(output: str) -> QemuVersion | None:
    """Pull the version out of ``--version`` output, or None if it is not there."""
    match = _VERSION.search(output or "")
    if not match:
        return None
    release = tuple(int(part) for part in match.group("version").split("."))
    return QemuVersion(
        raw=(output or "").strip().splitlines()[0].strip(),
        release=release,
        build=match.group("detail"),
    )


def _as_tuple(text: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in text.strip().split("."))
    except ValueError:
        return ()


@dataclass(frozen=True)
class Capability:
    """One thing the build either can or cannot do, and what follows from it."""

    key: str
    label: str
    available: bool
    #: What was observed. Shown verbatim — "‑tpmdev: invalid option" is more
    #: use to somebody debugging than "unsupported".
    detail: str
    #: What the user loses. None when nothing does.
    consequence: str | None = None


@dataclass
class QemuSupport:
    """The whole picture: which build, whether it is in range, what it can do."""

    version: QemuVersion | None
    #: "ok" | "untested" | "too-old" | "prerelease" | "unknown"
    status: str
    warnings: list[str] = field(default_factory=list)
    capabilities: list[Capability] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        """Whether the build is inside the tested range. Advisory only."""
        return self.status == "ok"

    def capability(self, key: str) -> Capability | None:
        return next((c for c in self.capabilities if c.key == key), None)

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version.text if self.version else None,
            "raw": self.version.raw if self.version else None,
            "prerelease": bool(self.version and self.version.is_prerelease),
            "status": self.status,
            "supported": self.supported,
            "warnings": list(self.warnings),
            "capabilities": [
                {
                    "key": c.key,
                    "label": c.label,
                    "available": c.available,
                    "detail": c.detail,
                    "consequence": c.consequence,
                }
                for c in self.capabilities
            ],
        }


def _probe(binary: str, args: list[str], timeout: int) -> tuple[int, str]:
    """Run a read-only QEMU query. Never raises — a probe that fails is data."""
    try:
        completed = subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def _tpm_capability(binary: str, timeout: int) -> Capability:
    """Whether this build can give a guest a TPM.

    On Windows the answer is always no, and structurally so: QEMU's build system
    refuses the feature there outright. Worth probing rather than hardcoding by
    platform anyway — the same backend can answer for a Linux host, and a probe
    that reads the real binary cannot drift from it.
    """
    code, output = _probe(binary, ["-tpmdev", "help"], timeout)
    unsupported = "invalid option" in output.lower() or code != 0
    if unsupported:
        return Capability(
            key="tpm",
            label="TPM 2.0 emulation",
            available=False,
            detail=(
                output.splitlines()[-1].strip()
                if output.strip()
                else "-tpmdev is not accepted by this build"
            ),
            consequence=(
                "Windows 11 cannot be installed — it requires TPM 2.0. QEMU "
                "disables TPM emulation on Windows hosts entirely, so no "
                "upgrade adds it. Windows Server and Windows 10 are unaffected."
                if sys.platform == "win32"
                else "Windows 11 cannot be installed; it requires TPM 2.0. "
                "Install swtpm and use a QEMU built with --enable-tpm."
            ),
        )
    return Capability(
        key="tpm",
        label="TPM 2.0 emulation",
        available=True,
        detail="-tpmdev accepted; an external swtpm is still required at launch",
    )


def _device_capability(binary: str, timeout: int) -> Capability:
    """Whether the devices a Windows guest needs are compiled in."""
    _, output = _probe(binary, ["-device", "help"], timeout)
    lowered = output.lower()
    missing = [name for name in ("ich9-ahci", "e1000e") if name not in lowered]
    if missing:
        return Capability(
            key="windows_devices",
            label="Windows guest devices (AHCI, e1000e)",
            available=False,
            detail=f"not offered by this build: {', '.join(missing)}",
            consequence=(
                "Windows guests cannot be launched: Setup has no inbox driver "
                "for the paravirtualised alternatives."
            ),
        )
    return Capability(
        key="windows_devices",
        label="Windows guest devices (AHCI, e1000e)",
        available=True,
        detail="ich9-ahci and e1000e are both available",
    )


def _resolve(binary: str) -> Path | None:
    """Where a configured binary name actually lands, following PATH order."""
    located = shutil.which(binary)
    if located:
        return Path(located).resolve()
    candidate = Path(binary)
    return candidate.resolve() if candidate.is_file() else None


def _toolchain_capability(settings: Settings, timeout: int) -> Capability:
    """Whether ``qemu-system-x86_64`` and ``qemu-img`` come from one install.

    PATH order is not a guarantee, and trusting it cost this project a whole
    phase. Multipass installs its own ``qemu-img`` and puts its ``bin``
    directory on the system PATH; on this host that shadowed the real one, so
    every image probe, overlay and snapshot ran through a stale 8.0.0-dirty
    binary while ``qemu-system-x86_64`` resolved from the actual QEMU install.
    Nothing reported a problem, because nothing was comparing the two — each
    tool answered ``--version`` for *itself* and both answers looked fine.

    The failure mode this guards against is nasty precisely because it is
    quiet: the two halves of the toolchain write and read the same qcow2 files,
    so a mismatch surfaces later as a corrupt overlay or an unparsable snapshot
    listing, a long way from the version skew that caused it.

    Directory *and* version are both compared. Same directory with different
    versions means a half-finished upgrade; same version from different
    directories is benign but still worth naming, because it means PATH is
    deciding something nobody chose.
    """
    system_path = _resolve(settings.qemu_system_binary)
    img_path = _resolve(settings.qemu_img_binary)

    if system_path is None or img_path is None:
        # Both, when both are missing. Naming only the first sends someone to
        # fix one path and hit the same warning again.
        missing = [
            name
            for name, path in (
                (settings.qemu_system_binary, system_path),
                (settings.qemu_img_binary, img_path),
            )
            if path is None
        ]
        return Capability(
            key="toolchain",
            label="QEMU toolchain consistency",
            available=False,
            detail=f"could not locate on PATH: {', '.join(missing)}",
            consequence=(
                "Disk images cannot be created or probed. Put QEMU on the "
                "backend's PATH, or set IAAS_QEMU_SYSTEM_BINARY and "
                "IAAS_QEMU_IMG_BINARY to full paths in the same install."
            ),
        )

    system_version = parse_version(_probe(str(system_path), ["--version"], timeout)[1])
    img_version = parse_version(_probe(str(img_path), ["--version"], timeout)[1])
    same_dir = system_path.parent == img_path.parent
    same_version = (
        system_version is not None
        and img_version is not None
        and system_version.release == img_version.release
        and system_version.build == img_version.build
    )

    if same_dir and same_version:
        return Capability(
            key="toolchain",
            label="QEMU toolchain consistency",
            available=True,
            detail=(
                f"qemu-system-x86_64 and qemu-img are both "
                f"{system_version.text if system_version else 'present'} "
                f"from {system_path.parent}"
            ),
        )

    def describe(path: Path, version: QemuVersion | None) -> str:
        return f"{path} ({version.raw if version else 'no version'})"

    reason = (
        "different versions" if same_dir
        else "different directories" if same_version
        else "different directories and versions"
    )
    return Capability(
        key="toolchain",
        label="QEMU toolchain consistency",
        available=False,
        detail=(
            f"{reason} — system: {describe(system_path, system_version)}; "
            f"image tool: {describe(img_path, img_version)}"
        ),
        consequence=(
            "These two write and read the same qcow2 files, so a version skew "
            "between them can corrupt overlays or produce snapshot output this "
            "build cannot parse — and it does so silently, long after the "
            "mismatch. A stray qemu-img earlier on PATH (Multipass ships one) "
            "is the usual cause. Set IAAS_QEMU_SYSTEM_BINARY and "
            "IAAS_QEMU_IMG_BINARY to full paths in the same install."
        ),
    )


#: First build measured to survive OVMF-through-``pflash`` under WHPX.
#:
#: The bug (QEMU killed instantly by "Failed to emulate MMIO access with
#: EmulatorReturnStatus: 2") was open upstream from 2019 and reproduced here on
#: 10.0.94. On 11.1.0 the same command line boots: firmware initialises the
#: adapter to 1280x800, reaches its console, and hands off to a real bootloader
#: — measured by booting Alpine to a kernel console through pflash OVMF, not by
#: reading a changelog.
#:
#: A threshold rather than a probe because the only honest probe is booting a
#: VM, and ``/health`` is polled by the dashboard. Two measured points bracket
#: it; anything between them is guessed, so the constant names the build that
#: was actually tested and the warning below says so.
_UEFI_WHPX_FIXED = (11, 1, 0)


def _uefi_capability(
    binary: str, accel: str | None, version: QemuVersion | None = None
) -> Capability:
    """Whether UEFI firmware is present, and usable with this accelerator.

    Two separate failures wearing one name. The firmware may simply not be
    installed; or it may be installed and unusable, because older QEMU could not
    emulate the MMIO accesses OVMF makes through a ``pflash`` device under WHPX.
    The second is the one that surprises people, since every file is exactly
    where it should be.
    """
    # The binary is usually a bare name found on PATH ("qemu-system-x86_64"),
    # and `Path(name).parent` for a bare name is `.` — the *backend's* working
    # directory, which has no firmware in it. Resolving through `which` first is
    # what makes this look where QEMU actually lives. Found the hard way: this
    # reported firmware missing on a host where it was measurably present.
    located = shutil.which(binary)
    root = Path(located).resolve().parent if located else Path(binary).resolve().parent
    search = [root, root / "share"]
    found = next(
        (
            candidate
            for directory in search
            for candidate in (
                directory / "edk2-x86_64-code.fd",
                directory / "OVMF_CODE.fd",
            )
            if candidate.is_file()
        ),
        None,
    )
    if found is None:
        return Capability(
            key="uefi",
            label="UEFI firmware (OVMF)",
            available=False,
            detail="no edk2-x86_64-code.fd or OVMF_CODE.fd beside the binary",
            consequence="Guests that require UEFI cannot boot; legacy BIOS still works.",
        )
    if accel == "whpx" and not (version and version.release >= _UEFI_WHPX_FIXED):
        return Capability(
            key="uefi",
            label="UEFI firmware (OVMF)",
            available=False,
            detail=f"present at {found.name}, but unusable with WHPX on this build",
            consequence=(
                "WHPX cannot emulate OVMF's pflash MMIO access and the VM dies "
                "at once (a QEMU bug open since 2019). Fixed as of QEMU "
                f"{'.'.join(str(p) for p in _UEFI_WHPX_FIXED)}, so upgrading is "
                "the fix; until then UEFI guests need software emulation. "
                "Legacy BIOS is unaffected, and neither Windows Server nor "
                "Windows 10 requires UEFI."
            ),
        )
    # Firmware present and, on a new enough build, genuinely working — and still
    # `available=False`, because this field answers "can an instance boot UEFI
    # here", not "does the file exist". The engine emits no `-drive if=pflash`
    # at all, so the honest answer is no. Reporting True described the host and
    # let the reader infer a product feature that does not exist.
    #
    # No `consequence`, deliberately: that field feeds /health warnings, which
    # are about defects in *this build* and are meant to be acted on. A feature
    # nobody has written yet is a roadmap item, not a build defect, and putting
    # it in the warning list would blur the two. The detail below carries it.
    if accel == "whpx":
        return Capability(
            key="uefi",
            label="UEFI firmware (OVMF)",
            available=False,
            detail=(
                f"{found.name} present, and pflash works under WHPX on QEMU "
                f"{version.text if version else '?'} (broken before "
                f"{'.'.join(str(p) for p in _UEFI_WHPX_FIXED)}) — detected, not "
                "wired into launches: the engine emits no pflash arguments, so "
                "every instance boots legacy BIOS"
            ),
        )
    return Capability(
        key="uefi",
        label="UEFI firmware (OVMF)",
        available=False,
        detail=(
            f"{found.name} present — detected, not wired into launches: the "
            "engine emits no pflash arguments, so every instance boots legacy BIOS"
        ),
    )


def probe_support(settings: Settings, accel: str | None = None) -> QemuSupport:
    """Version plus capabilities for the configured QEMU. Never raises."""
    binary = settings.qemu_system_binary
    timeout = settings.cli_timeout_seconds

    code, output = _probe(binary, ["--version"], timeout)
    version = parse_version(output) if code == 0 else None

    if version is None:
        return QemuSupport(
            version=None,
            status="unknown",
            warnings=[
                f"Could not read a version from '{binary}'. Everything below is "
                f"unverified. ({output.splitlines()[0] if output else 'no output'})"
            ],
        )

    minimum = _as_tuple(settings.qemu_version_min)
    tested = _as_tuple(settings.qemu_version_max_tested)
    warnings: list[str] = []

    if minimum and version.release < minimum:
        status = "too-old"
        warnings.append(
            f"QEMU {version.text} is below the minimum this build is written "
            f"against ({settings.qemu_version_min}). Expect missing devices and "
            f"command-line options; upgrading is the fix."
        )
    elif version.is_prerelease:
        # Deliberately ahead of the "untested" check: a snapshot's number is not
        # a claim anyone else can act on, so saying "newer than tested" about it
        # would be precise and useless.
        status = "prerelease"
        # `raw` already begins "QEMU emulator version …", so it is not prefixed
        # again here the way the bare numbers are elsewhere.
        warnings.append(
            f"{version.raw} is a development build, not a release. It may "
            f"behave differently from any tested version, and nobody else can "
            f"install this exact build — pin a release for anything shared."
        )
    elif tested and version.release > tested:
        status = "untested"
        warnings.append(
            f"QEMU {version.text} is newer than the most recent version this "
            f"build has been tested against ({settings.qemu_version_max_tested}). "
            f"It will probably work; if something behaves oddly, that gap is the "
            f"first thing to mention in a bug report."
        )
    else:
        status = "ok"

    capabilities = [
        _toolchain_capability(settings, timeout),
        _tpm_capability(binary, timeout),
        _uefi_capability(binary, accel, version),
        _device_capability(binary, timeout),
    ]
    warnings.extend(
        f"{capability.label}: {capability.consequence}"
        for capability in capabilities
        if not capability.available and capability.consequence
    )

    return QemuSupport(
        version=version, status=status, warnings=warnings, capabilities=capabilities
    )


#: Memo for :func:`cached_support`, keyed on everything that could change the
#: answer. A plain dict rather than ``lru_cache`` because the value has to be
#: computed from the *caller's* settings — see below.
_SUPPORT_CACHE: dict[tuple[str, str, str, str, str | None], QemuSupport] = {}


def cached_support(settings: Settings, accel: str | None = None) -> QemuSupport:
    """``probe_support`` memoised on the inputs that could change its answer.

    Three subprocesses per call is nothing at startup and too much for a health
    endpoint the dashboard polls.

    The memo is keyed on the settings that feed it *and computed from them*.
    An earlier version cached on the key but then called ``get_settings()`` to
    do the work, which quietly probed the real configuration no matter which
    settings the caller passed — exactly the kind of thing that makes a test
    reach outside its tmp_path.
    """
    key = (
        settings.qemu_system_binary,
        # Part of the key because the toolchain check reads it. Left out, a test
        # pointing IAAS_QEMU_IMG_BINARY somewhere new would get a cached answer
        # computed for the previous one.
        settings.qemu_img_binary,
        settings.qemu_version_min,
        settings.qemu_version_max_tested,
        accel,
    )
    if key not in _SUPPORT_CACHE:
        _SUPPORT_CACHE[key] = probe_support(settings, accel)
    return _SUPPORT_CACHE[key]
