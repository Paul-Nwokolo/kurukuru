"""
Build the shipped artefacts from a clean checkout.

Runnable by somebody who is not the author, which is the actual requirement —
so every input is pinned, every check explains itself, and nothing depends on
the state of the machine that happened to run it last.

    python tools/build_installer.py --qemu "C:\\Program Files\\qemu"

Stages, in order:

1. **Refuse an unbuildable path.** Windows' 260-character limit has now broken
   this project three separate times. It is checked first because a failure
   here after twenty minutes of building is a failure that wasted twenty
   minutes.
2. **Build the dashboard** (``npm run build``).
3. **Freeze the backend** (PyInstaller, onedir).
4. **Collect QEMU**, trimmed to what an x86_64 host actually needs, with a
   SHA-256 recorded for every file.
5. **Verify** that manifest against what is on disk, immediately before
   packaging, so what ships is what was measured.

Signing is deliberately not here. It is a step applied to the finished
executable, so adding it later changes this file by an append rather than a
rewrite.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
# 1. The path budget
# --------------------------------------------------------------------------- #
#: Windows' classic maximum path length. Not advisory: with long paths
#: disabled, the Win32 file APIs return ERROR_FILENAME_EXCED_RANGE (206) and
#: the call simply fails.
MAX_PATH = 260

#: The longest path *inside* the build tree, measured rather than guessed:
#:
#:   dist\\kurukuru-backend\\_internal\\setuptools\\_vendor\\
#:       importlib_metadata-8.7.1.dist-info\\licenses\\LICENSE
#:
#: which is 102 characters. That exact path is what failed the first frozen
#: build of this project, from a 159-character build root — 262 total, over the
#: limit by **two characters**.
_MEASURED_DEEPEST = 102

#: Headroom over the measured worst case, because the deepest path is a
#: property of the dependency tree and a future package will be worse. A
#: dependency that adds 18 characters of nesting should fail this check with an
#: explanation, not fail the build with WinError 206.
_HEADROOM = 24

#: The most a build root may be. Everything above, applied.
MAX_BUILD_ROOT = MAX_PATH - 1 - _MEASURED_DEEPEST - _HEADROOM  # 133


class BuildError(RuntimeError):
    """Something is wrong with the build inputs. Always says what to do."""


def long_paths_enabled() -> bool:
    """Whether this machine has opted out of the 260-character limit.

    A per-machine registry flag, off by default on Windows 11. When it is on
    the limit genuinely does not apply and enforcing it would be superstition —
    so the check reports that rather than pretending.
    """
    if sys.platform != "win32":
        return True
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            return bool(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
    except OSError:
        return False


def check_build_root(root: Path) -> None:
    """Refuse a build root long enough to break the build. Loudly, and first.

    **This project's most repeated failure.** It has bitten three times: once
    creating the virtualenv, once on the first PyInstaller build, and once in
    the Phase 13 notes. Every time the symptom was an unrelated-looking error
    about a file deep inside a dependency, and every time the cause was the
    length of a directory nobody was thinking about.

    So it is checked before anything is built, and the message carries the
    arithmetic rather than the advice — "use a shorter path" is not actionable
    when you cannot see how much shorter.
    """
    root = root.resolve()
    length = len(str(root))
    if length <= MAX_BUILD_ROOT:
        return

    if long_paths_enabled():
        print(
            f"note: build root is {length} characters, over the "
            f"{MAX_BUILD_ROOT}-character budget — but long paths are enabled on "
            f"this machine, so the limit does not apply. Continuing.",
            file=sys.stderr,
        )
        return

    raise BuildError(
        f"The build root is too long for Windows.\n"
        f"\n"
        f"    path     {root}\n"
        f"    length   {length} characters\n"
        f"    budget   {MAX_BUILD_ROOT} characters\n"
        f"    over by  {length - MAX_BUILD_ROOT}\n"
        f"\n"
        f"Windows' limit is {MAX_PATH} characters for a whole path, and the "
        f"deepest file this build creates sits {_MEASURED_DEEPEST} characters "
        f"below the root (a nested .dist-info/licenses/ inside a vendored "
        f"dependency), with {_HEADROOM} characters of headroom reserved for the "
        f"next dependency that nests deeper.\n"
        f"\n"
        f"This is not a warning that can be worked around by retrying: the file "
        f"API returns ERROR_FILENAME_EXCED_RANGE and the build fails partway "
        f"through, with an error naming a file in a package you have never "
        f"heard of.\n"
        f"\n"
        f"Either build from a shorter path:\n"
        f"    python tools/build_installer.py --build-root C:\\kk-build\n"
        f"\n"
        f"or enable long path support machine-wide (needs an elevated shell, "
        f"and a reboot for some tools):\n"
        f"    Set-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
        f"LongPathsEnabled 1"
    )


# --------------------------------------------------------------------------- #
# 4. QEMU: what to bundle, and proving it is what was tested
# --------------------------------------------------------------------------- #
#: The QEMU release this build is pinned to. Recorded in the manifest and
#: checked against the version the collected binaries actually report, because
#: a pin nobody verifies is a comment.
PINNED_QEMU_VERSION = "11.1.0"

#: Firmware and option ROMs an x86_64 guest can actually load. Named explicitly
#: rather than copied wholesale: the full share/ directory carries ARM, RISC-V
#: and PPC firmware totalling hundreds of megabytes that this product can never
#: reach, because it ships one emulator.
QEMU_FIRMWARE = (
    "bios-256k.bin", "bios.bin", "bios-microvm.bin",
    "vgabios-stdvga.bin", "vgabios-virtio.bin", "vgabios-vmware.bin",
    "vgabios-qxl.bin", "vgabios-bochs-display.bin", "vgabios-ramfb.bin",
    "vgabios.bin", "kvmvapic.bin", "linuxboot.bin", "linuxboot_dma.bin",
    "pvh.bin", "multiboot.bin", "multiboot_dma.bin", "sgabios.bin",
    "efi-e1000e.rom", "efi-e1000.rom", "efi-virtio.rom", "efi-rtl8139.rom",
    "efi-ne2k_pci.rom", "efi-pcnet.rom", "efi-vmxnet3.rom",
    "pxe-e1000e.rom", "pxe-e1000.rom", "pxe-virtio.rom",
    "edk2-x86_64-code.fd", "edk2-i386-vars.fd", "edk2-i386-code.fd",
    "edk2-x86_64-secure-code.fd",
)

#: QEMU's own licence texts. **Not optional.** The binaries are GPLv2 and
#: distributing them obliges shipping these, so they are collected with the
#: same machinery as the executables and a missing one fails the build.
QEMU_LICENCES = ("COPYING", "COPYING.LIB")

MANIFEST_NAME = "qemu-manifest.json"


@dataclass
class Manifest:
    """What was collected, and the SHA-256 of every byte of it.

    **Why a checksum manifest rather than trusting the signature.** The upstream
    Windows build is signed with an expired certificate, which its own download
    page says. An expired signature is not a weaker signature; it is one that
    cannot be validated, so treating it as assurance would be theatre.

    What is actually wanted is narrower anyway: not "who published this" but
    "is this the same binary the suite and the live launch were run against".
    A checksum answers that exactly, and it is the same question Phase 13 could
    not answer when a shadowed ``qemu-img`` from an unrelated install produced
    results nobody could reproduce.
    """

    qemu_version: str
    source: str
    built_at: str
    files: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "qemu_version": self.qemu_version,
                "source": self.source,
                "built_at": self.built_at,
                "file_count": len(self.files),
                "files": dict(sorted(self.files.items())),
            },
            indent=2,
        )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def qemu_version_of(binary: Path) -> str:
    """The version the collected emulator reports about itself.

    Run rather than assumed. The whole point of pinning is that the bundle is a
    known build, and a source directory that quietly contains a different one is
    exactly the mistake the pin exists to catch.
    """
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise BuildError(f"{binary} would not run: {result.stderr.strip()}")
    first = result.stdout.splitlines()[0] if result.stdout else ""
    # "QEMU emulator version 11.1.0 (v11.1.0-12130-ge470268ff4)"
    for token in first.split():
        if token[:1].isdigit():
            return token
    raise BuildError(f"Could not read a version out of: {first!r}")


def collect_qemu(source: Path, target: Path) -> Manifest:
    """Copy the x86_64 subset of a QEMU install, hashing as it goes.

    Trimmed hard, and the arithmetic is why: a full Windows QEMU install is
    ~1.9 GB across ~3,400 files, of which 60 emulators for other architectures
    are 713 MB and a *guest* driver ISO (``virtio-win.iso``) is another 693 MB.
    Neither is reachable from a product that launches one architecture on the
    host. The subset is ~190 MB.
    """
    if not source.is_dir():
        raise BuildError(
            f"No QEMU install at {source}.\n"
            f"Install the pinned build first:\n"
            f"    https://qemu.weilnetz.de/w64/qemu-w64-setup-20260811.exe\n"
            f"then pass --qemu with its install directory."
        )

    emulator = source / "qemu-system-x86_64.exe"
    if not emulator.is_file():
        raise BuildError(f"{emulator} is missing; is {source} really a QEMU install?")

    found = qemu_version_of(emulator)
    if found != PINNED_QEMU_VERSION:
        raise BuildError(
            f"QEMU version mismatch.\n"
            f"    pinned    {PINNED_QEMU_VERSION}\n"
            f"    found     {found}  (at {source})\n"
            f"\n"
            f"The pin is what makes 'tested' mean anything. Either install the "
            f"pinned build, or — if you have deliberately moved to a newer one "
            f"— run the suite and a live Windows launch against it, then update "
            f"PINNED_QEMU_VERSION here and qemu_version_max_tested in "
            f"kurukuru/config.py together."
        )

    if target.exists():
        shutil.rmtree(target)
    (target / "share" / "keymaps").mkdir(parents=True)

    manifest = Manifest(
        qemu_version=found,
        source=str(source),
        built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    def take(src: Path, relative: str) -> None:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, destination)
        manifest.files[relative.replace("\\", "/")] = sha256(destination)

    take(emulator, "qemu-system-x86_64.exe")
    take(source / "qemu-img.exe", "qemu-img.exe")
    for dll in sorted(source.glob("*.dll")):
        take(dll, dll.name)

    for name in QEMU_LICENCES:
        licence = source / name
        if not licence.is_file():
            raise BuildError(
                f"{licence} is missing. QEMU is GPLv2 and its licence text must "
                f"ship with the binaries; a bundle without it is not "
                f"distributable."
            )
        take(licence, name)

    share = source / "share"
    for name in QEMU_FIRMWARE:
        blob = share / name
        if blob.is_file():
            take(blob, f"share/{name}")
    edk2 = share / "edk2-licenses.txt"
    if edk2.is_file():
        take(edk2, "share/edk2-licenses.txt")
    for keymap in sorted((share / "keymaps").glob("*")):
        if keymap.is_file():
            take(keymap, f"share/keymaps/{keymap.name}")

    (target / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
    return manifest


def verify_qemu(target: Path) -> int:
    """Re-hash everything the manifest names. Returns the number checked.

    Run immediately before packaging rather than only at collection, so that
    what ships is what was measured — a build that collected correctly and then
    had a file replaced, truncated by a full disk, or quarantined by antivirus
    would otherwise package the damage silently.
    """
    manifest_path = target / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BuildError(f"No {MANIFEST_NAME} in {target}; nothing to verify against.")

    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    problems: list[str] = []
    for relative, expected in sorted(recorded.items()):
        path = target / relative
        if not path.is_file():
            problems.append(f"  missing:  {relative}")
            continue
        actual = sha256(path)
        if actual != expected:
            problems.append(
                f"  changed:  {relative}\n"
                f"      expected {expected}\n"
                f"      found    {actual}"
            )

    # A file present but unrecorded is as much a problem as one missing: it
    # would ship without ever having been checked.
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative = path.relative_to(target).as_posix()
        if relative not in recorded:
            problems.append(f"  unrecorded: {relative}")

    if problems:
        raise BuildError(
            "The bundled QEMU does not match its manifest:\n"
            + "\n".join(problems)
            + "\n\nRe-collect it rather than shipping this."
        )
    return len(recorded)


# --------------------------------------------------------------------------- #
# 2 & 3. The parts that are just builds
# --------------------------------------------------------------------------- #
def run(command: list[str], *, cwd: Path) -> None:
    print(f"  $ {subprocess.list2cmdline(command)}")
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise BuildError(
            f"{command[0]} exited {result.returncode} in {cwd}. "
            f"The output above is the actual error."
        )


def build_dashboard(out: Path) -> Path:
    frontend = REPO / "frontend"
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise BuildError("npm is not on PATH; the dashboard cannot be built.")
    run([npm, "ci"], cwd=frontend)
    run([npm, "run", "build"], cwd=frontend)

    dist = frontend / "dist"
    if not (dist / "index.html").is_file():
        raise BuildError(f"{dist} has no index.html; the dashboard build produced nothing.")

    # A bundle with an origin welded into it is broken for every user whose
    # origin differs from the build machine's, and it looks perfect on the
    # machine that built it. That shipped once; it is checked here so it cannot
    # ship again from an automated build that skipped `npm run verify`.
    run([npm, "run", "check:origin"], cwd=frontend)
    target = out / "dashboard"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(dist, target)
    return target


def freeze_backend(out: Path, work: Path) -> Path:
    entry = out / "kurukuru-entry.py"
    entry.write_text(
        '"""Generated entry point: what the installed backend launches."""\n'
        "import sys\n\n"
        "from kurukuru.cli.main import main\n\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main() or 0)\n",
        encoding="utf-8",
    )
    run(
        [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
            "--name", "kurukuru",
            "--distpath", str(out / "dist"),
            "--workpath", str(work),
            "--specpath", str(work),
            # onedir, never onefile. Onefile unpacks itself to a temp directory
            # on every launch, which is behaviourally what a dropper does and is
            # the mode with the antivirus reputation — for a startup saving of
            # nothing, since this application's start time is dominated by a
            # QEMU capability probe rather than by Python.
            "--collect-all", "kurukuru",
            "--collect-all", "uvicorn",
            "--collect-all", "fastapi",
            "--collect-all", "pydantic",
            "--collect-all", "pydantic_settings",
            "--collect-all", "sqlmodel",
            "--collect-all", "sqlalchemy",
            "--collect-all", "pycdlib",
            "--collect-all", "argon2",
            "--hidden-import", "uvicorn.logging",
            "--hidden-import", "uvicorn.lifespan.on",
            str(entry),
        ],
        cwd=REPO / "backend",
    )
    frozen = out / "dist" / "kurukuru"
    if not frozen.is_dir():
        raise BuildError(f"PyInstaller produced nothing at {frozen}.")
    return frozen


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--build-root", type=Path, default=Path("C:/kk-build"),
        help="Where to build. Kept short on purpose; see check_build_root.",
    )
    parser.add_argument(
        "--qemu", type=Path, default=Path("C:/Program Files/qemu"),
        help="A QEMU install to bundle from.",
    )
    parser.add_argument(
        "--skip-dashboard", action="store_true",
        help="Reuse an existing frontend/dist instead of rebuilding it.",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Run the pre-flight checks and stop. Fast, and safe to run anywhere.",
    )
    args = parser.parse_args(argv)

    try:
        print("[1/5] Checking the build root")
        check_build_root(args.build_root)
        print(f"      {args.build_root} — {len(str(args.build_root.resolve()))}"
              f"/{MAX_BUILD_ROOT} characters")
        if args.check_only:
            print("      --check-only, stopping here.")
            return 0

        out = args.build_root / "stage"
        out.mkdir(parents=True, exist_ok=True)

        print("[2/5] Building the dashboard")
        if args.skip_dashboard:
            print("      skipped")
        else:
            print(f"      -> {build_dashboard(out)}")

        print("[3/5] Freezing the backend")
        print(f"      -> {freeze_backend(out, args.build_root / 'work')}")

        print("[4/5] Collecting QEMU")
        manifest = collect_qemu(args.qemu, out / "qemu")
        print(f"      QEMU {manifest.qemu_version}, {len(manifest.files)} files hashed")

        print("[5/5] Verifying the bundle against its manifest")
        print(f"      {verify_qemu(out / 'qemu')} files verified")

        print(f"\nStaged at {out}")
        return 0
    except BuildError as exc:
        print(f"\nBuild failed.\n\n{exc}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
