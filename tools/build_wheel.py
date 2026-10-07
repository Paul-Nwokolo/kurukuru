"""
Build the Python wheel — the Linux install artefact — and check what is in it.

    python tools/build_wheel.py --out dist/
    python tools/build_wheel.py --out dist/ --skip-dashboard   # reuse frontend/dist

The wheel is what ``pipx install`` installs on Linux (DECISIONS #71). It carries
the built dashboard inside the package, because a wheel install has no checkout
to find ``frontend/dist`` in — the same lesson as the Windows build, whose
installed app once answered every dashboard URL with a 404 because it looked
for the bundle in the wrong place.

QEMU is deliberately not in it. On Linux the distribution's QEMU is used
(DECISIONS #70), which also means there is no bundle to go stale across an
upgrade the way DECISIONS #66 found on Windows: pipx replaces the virtualenv
wholesale.

Stages:

1. Build the dashboard (``npm run build``) unless ``--skip-dashboard``.
2. Copy it into ``backend/kurukuru/_dashboard`` — a build-time copy, git-ignored.
3. ``pip wheel --no-deps`` the backend, with no build isolation so it needs no
   network.
4. Remove the copy, whatever happened, so a checkout never serves a stale one.
5. **Inspect the wheel**: the dashboard's ``index.html`` and hashed assets are
   in it, no test or cache files are, and its version is the product's. A build
   that exits 0 is not evidence the artefact is right.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
FRONTEND_DIST = REPO / "frontend" / "dist"
PACKAGED = BACKEND / "kurukuru" / "_dashboard"


class BuildError(RuntimeError):
    pass


def product_version() -> str:
    sys.path.insert(0, str(BACKEND))
    try:
        from kurukuru.product import VERSION
    finally:
        sys.path.pop(0)
    return VERSION


def build_dashboard() -> None:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if npm is None:
        raise BuildError("npm is needed to build the dashboard (or pass --skip-dashboard).")
    result = subprocess.run([npm, "run", "build"], cwd=REPO / "frontend", check=False, timeout=600)
    if result.returncode != 0:
        raise BuildError(f"npm run build exited {result.returncode}.")


def check_wheel(wheel: Path, version: str) -> list[str]:
    """Problems with the built wheel; empty means it is the artefact it should be."""
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()
    problems = []
    if f"kurukuru-{version}-" not in wheel.name:
        problems.append(f"{wheel.name} is not version {version}")
    if "kurukuru/_dashboard/index.html" not in names:
        problems.append("the dashboard's index.html is not in the wheel")
    if not any(n.startswith("kurukuru/_dashboard/assets/") and n.endswith(".js") for n in names):
        problems.append("no hashed dashboard assets in the wheel")
    for n in names:
        if "/tests/" in n or n.startswith("tests/") or "__pycache__" in n or n.endswith(".pyc"):
            problems.append(f"should not ship: {n}")
    if not any(n.endswith(".dist-info/entry_points.txt") for n in names):
        problems.append("no entry_points.txt: `kurukuru` would not be on PATH after install")
    return problems


def build(out: Path, *, skip_dashboard: bool = False) -> Path:
    version = product_version()
    if not skip_dashboard:
        build_dashboard()
    if not (FRONTEND_DIST / "index.html").is_file():
        raise BuildError(f"No built dashboard at {FRONTEND_DIST}; run without --skip-dashboard.")

    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("kurukuru-*.whl"):
        old.unlink()  # a build output directory, never anything else
    if PACKAGED.exists():
        shutil.rmtree(PACKAGED)  # only ever our own build-time copy
    shutil.copytree(FRONTEND_DIST, PACKAGED)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-w", str(out), str(BACKEND)],
            check=False, timeout=600,
        )
        if result.returncode != 0:
            raise BuildError(f"pip wheel exited {result.returncode}.")
    finally:
        shutil.rmtree(PACKAGED, ignore_errors=True)
        # setuptools leaves a build/ beside the project; it holds a copy of
        # _dashboard and would be picked up by the next build's package-data.
        shutil.rmtree(BACKEND / "build", ignore_errors=True)

    wheels = sorted(out.glob(f"kurukuru-{version}-*.whl"))
    if len(wheels) != 1:
        raise BuildError(f"expected one kurukuru-{version} wheel in {out}, found {wheels}")
    problems = check_wheel(wheels[0], version)
    if problems:
        raise BuildError("The wheel is not right:\n  " + "\n  ".join(problems))
    return wheels[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "dist")
    parser.add_argument("--skip-dashboard", action="store_true")
    args = parser.parse_args(argv)
    try:
        wheel = build(args.out, skip_dashboard=args.skip_dashboard)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{wheel}  ({wheel.stat().st_size / 1024:.0f} KB) - dashboard included, contents checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
