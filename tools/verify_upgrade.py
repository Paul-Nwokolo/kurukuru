"""
Check that an installed Kurukuru *is* the build that produced the installer.

Run by the release workflow after installing the new build **over the previous
release**, which is the path an actual user takes. Two properties, and both are
needed:

1. **The installed tree is the build, exactly.** Every file the installer
   script's ``[Files]`` section puts under the install directory must be there
   and byte-identical to its source in the build, and nothing else may be —
   bar Inno's own ``unins000.exe``/``.dat``. Not a file count: a count passes
   any future layout in which a stale file and a missing one cancel out, and
   "which version loaded" goes back to depending on import order. The expected
   set is read from ``kurukuru.iss`` itself, so it cannot drift from what the
   installer installs.

2. **The installed executable reports the pinned framework.** ``kurukuru
   version --json`` carries ``framework``: the FastAPI and Starlette the process
   actually *imported*, read from the modules rather than from package
   metadata. Compared against the pins in ``backend/pyproject.toml``.

Why this exists (DECISIONS #66): until 0.1.4 the installer copied over the top
and removed nothing, so an upgrade layered the new build over the old one and
which version of a dependency loaded was undefined. 0.1.2 -> 0.1.4 left
FastAPI 0.115.12's metadata beside 0.120.4's. The security fix took effect on
that machine because of how PyInstaller happens to resolve ``_internal``, not
because anything guaranteed it.

``--expect-fail`` inverts the verdict, for the negative control: run against the
*previous* release before upgrading, the check must find problems — otherwise
it is a check that passes everything.

Usage:
    python tools/verify_upgrade.py --install-dir C:\\kk-upgrade --stage C:\\kk-build\\stage
    python tools/verify_upgrade.py --install-dir C:\\kk-upgrade --stage C:\\kk-build\\stage --expect-fail
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ISS = REPO / "packaging" / "windows" / "kurukuru.iss"
PYPROJECT = REPO / "backend" / "pyproject.toml"

#: Written by Inno itself, not by [Files]; their presence is expected.
INNO_OWN = {"unins000.exe", "unins000.dat"}


def _files_entries(iss: Path) -> list[str]:
    entries, inside = [], False
    for line in iss.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped == "[Files]"
            continue
        if inside and stripped and not stripped.startswith(";"):
            entries.append(stripped)
    return entries


def expected_tree(stage: Path, iss: Path = ISS) -> dict[str, Path]:
    """Installed relative path (lower-cased, ``/``-separated) -> source file."""
    expected: dict[str, Path] = {}
    for entry in _files_entries(iss):
        source = re.search(r'Source:\s*"([^"]+)"', entry).group(1)
        dest = re.search(r'DestDir:\s*"([^"]+)"', entry).group(1)
        recursive = "recursesubdirs" in entry

        source = source.replace("{#StageDir}", str(stage))
        base = Path(source)
        if not base.is_absolute():
            base = iss.parent / source  # e.g. startup-task.ps1, beside the script
        dest_rel = dest.replace("{app}", "").lstrip("\\/").replace("\\", "/")

        if base.name == "*":
            root = base.parent
            files = root.rglob("*") if recursive else root.glob("*")
            for f in files:
                if f.is_file():
                    rel = f.relative_to(root).as_posix()
                    expected[_key(f"{dest_rel}/{rel}" if dest_rel else rel)] = f
        else:
            expected[_key(f"{dest_rel}/{base.name}" if dest_rel else base.name)] = base
    return expected


def _key(rel: str) -> str:
    return rel.replace("\\", "/").lower()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_problems(install_dir: Path, expected: dict[str, Path]) -> list[str]:
    installed = {
        _key(p.relative_to(install_dir).as_posix()): p
        for p in install_dir.rglob("*")
        if p.is_file()
    }
    problems = []
    for key, source in sorted(expected.items()):
        target = installed.get(key)
        if target is None:
            problems.append(f"missing: {key}")
        elif _sha256(target) != _sha256(source):
            problems.append(f"differs from the build: {key}")
    for key in sorted(set(installed) - set(expected)):
        if key not in INNO_OWN:
            problems.append(f"not part of this build: {key}")
    return problems


def pinned_framework(pyproject: Path = PYPROJECT) -> dict[str, str]:
    text = pyproject.read_text(encoding="utf-8")
    return dict(re.findall(r'"(fastapi|starlette)==([^"]+)"', text))


def framework_problems(reported: object, pinned: dict[str, str]) -> list[str]:
    if not isinstance(reported, dict):
        return [f"the installed executable does not report which framework it loaded "
                f"(got {reported!r}) - not this build"]
    return [
        f"{name}: loaded {reported.get(name)!r}, pinned {want!r}"
        for name, want in sorted(pinned.items())
        if reported.get(name) != want
    ]


def reported_framework(exe: Path) -> object:
    """``framework`` from the installed executable's ``version --json``.

    Pointed at a port nothing listens on and a throwaway state directory: the
    command reports an unreachable API as a null rather than failing, and this
    must not touch whatever real install the machine has.
    """
    with tempfile.TemporaryDirectory() as state:
        env = {**os.environ, "KURUKURU_STATE_DIR": state,
               "KURUKURU_API_URL": "http://127.0.0.1:9"}
        proc = subprocess.run([str(exe), "version", "--json"], capture_output=True,
                              text=True, env=env, timeout=120)
    try:
        return json.loads(proc.stdout).get("framework")
    except (json.JSONDecodeError, AttributeError):
        return f"unparseable output (exit {proc.returncode}): {proc.stdout[-200:]!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True,
                        help="The build's stage directory (build_installer.py --build-root/stage).")
    parser.add_argument("--expect-fail", action="store_true",
                        help="Negative control: succeed only if problems are found.")
    args = parser.parse_args(argv)

    problems = tree_problems(args.install_dir, expected_tree(args.stage))
    exe = args.install_dir / "kurukuru.exe"
    problems += framework_problems(
        reported_framework(exe) if exe.is_file() else None, pinned_framework()
    )

    for p in problems[:40]:
        print(f"  {p}")
    if len(problems) > 40:
        print(f"  ... and {len(problems) - 40} more")

    if args.expect_fail:
        if problems:
            print(f"negative control: {len(problems)} problem(s) found, as required")
            return 0
        print("negative control FAILED: an install that is not this build passed the check")
        return 1
    if problems:
        print(f"{len(problems)} problem(s): the installed tree is not this build")
        return 1
    print("the installed tree is this build, byte for byte, and loads the pinned framework")
    return 0


if __name__ == "__main__":
    sys.exit(main())
