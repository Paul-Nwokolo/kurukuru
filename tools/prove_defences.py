"""
Prove each defence test fails when its defence is broken.

A test that refuses something is only evidence if it has been seen to fail
when the refusal is gone. Phase 17 found three defences — TrustedHost, CORS,
the security headers — that could be deleted outright with the full suite
still green, because every test reached the app as a legitimate client
(DECISIONS #65). ``backend/tests/test_browser_defences.py`` closed that gap.
This keeps it closed: it applies each deliberate break below, runs the tests
that break must turn red, restores the file, and fails if any of those tests
*passed*.

**Named tests, not "something failed".** Each break lists the tests it must
fail, and every one of them must fail. Judging a break by the run's exit code
alone would accept a break caught by some unrelated test while the defence
test it was meant to prove sat green.

**One list, checked from the other side.** The registry below is hand-kept,
which is the shape this project distrusts. What makes it safe is
``tools/test_prove_defences.py``: every test function in the defence module
must appear in some break's ``must_fail``, so a defence test added without a
proven break turns the suite red rather than going unproven.

**What a break may touch.** Source files only, restored byte-for-byte in a
``finally``, even when pytest crashes. The one break that cannot be a source
edit — the vulnerable Starlette itself — installs 0.46.2 into a temporary
directory and puts it first on ``PYTHONPATH`` for that run. The venv is never
modified.

Usage (from the repository root, with the backend's venv active):
    python tools/prove_defences.py
    python tools/prove_defences.py --only cors headers
    python tools/prove_defences.py --list
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BACKEND = REPO / "backend"
DEFENCES = "tests/test_browser_defences.py"
NL = "\n"

#: A break whose tests hang has proven nothing, and must not stall CI.
BREAK_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class Break:
    """One deliberate break, and the tests it must turn red.

    ``must_fail`` holds ``module::function`` names. A parametrized function
    counts as failing only if *every* case of it fails — one surviving case
    means the break left part of the defence's claim unproven.
    """

    key: str
    what: str
    must_fail: tuple[str, ...]
    file: str | None = None
    old: str | None = None
    new: str | None = None
    #: Instead of an edit: a package spec installed onto PYTHONPATH for the run.
    shadow_package: str | None = None
    extra_args: tuple[str, ...] = field(default=())


def _d(name: str) -> str:
    return f"{DEFENCES}::{name}"


BREAKS: tuple[Break, ...] = (
    # --- the Phase 17 defence module -------------------------------------
    Break(
        "trustedhost-removed", "TrustedHostMiddleware not installed",
        (_d("test_a_rebound_host_is_refused_before_any_route_runs"),
         _d("test_a_rebound_host_cannot_open_the_console")),
        file="kurukuru/main.py",
        old="app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)",
        new="pass  # BROKEN: no Host check",
    ),
    Break(
        "trustedhost-refuses-all", "TrustedHost allow-list matches nothing real",
        (_d("test_the_loopback_names_are_accepted_with_or_without_a_port"),),
        file="kurukuru/main.py",
        old="app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)",
        new='app.add_middleware(TrustedHostMiddleware, allowed_hosts=["only.this.invalid"])',
    ),
    Break(
        "cors-wildcard", "CORS allows every origin",
        (_d("test_a_preflight_from_another_local_port_is_refused"),
         _d("test_a_simple_request_from_another_local_port_gets_no_cors_grant")),
        file="kurukuru/main.py",
        old="    allow_origins=settings.cors_origins,",
        new='    allow_origins=["*"], allow_origin_regex=".*",',
    ),
    Break(
        "headers-removed", "SecurityHeadersMiddleware not installed",
        (_d("test_security_headers_are_on_every_response"),),
        file="kurukuru/main.py",
        old="app.add_middleware(SecurityHeadersMiddleware, dashboard_root=_dashboard_root)",
        new="pass  # BROKEN: no security headers",
    ),
    Break(
        "range-unsupported", "assets served without Range support",
        (_d("test_an_ordinary_range_request_still_gets_a_partial_response"),),
        file="kurukuru/dashboard.py",
        old=NL.join(["            return FileResponse(", "                asset,"]),
        new=NL.join(["            return Response(", "                asset.read_bytes(),"]),
    ),
    Break(
        "starlette-0.46.2", "the vulnerable Starlette (PYSEC-2026-1942)",
        (_d("test_a_hostile_range_header_is_refused_cheaply"),),
        shadow_package="starlette==0.46.2",
    ),
    # --- the surfaces the brief named, guarded by older tests ------------
    Break(
        "console-first-byte", "console bridge drops the RFB greeting's first byte",
        ("tests/test_console.py::test_bytes_flow_from_vm_to_browser",),
        file="kurukuru/console.py",
        old="await websocket.send_bytes(data)",
        new="await websocket.send_bytes(data[1:])",
    ),
    Break(
        "guard-skips-images", "the auth guard lets /images through",
        ("tests/test_auth_coverage.py::test_every_route_is_closed_or_deliberately_public",),
        file="kurukuru/security.py",
        old="    conn.state.principal = auth.require_user(conn, db)",
        new="    conn.state.principal = None if path.startswith('/images') "
            "else auth.require_user(conn, db)",
        # Only some cases of the parametrized coverage test can fail here —
        # the /images ones — so this break is judged on "at least one".
        extra_args=("--any-case",),
    ),
    Break(
        "fastapi-default-docs", "FastAPI's own /docs and /openapi.json re-enabled",
        ("tests/test_auth_coverage.py::test_every_route_is_closed_or_deliberately_public",
         "tests/test_dashboard.py::test_every_api_route_is_under_the_prefix"),
        file="kurukuru/main.py",
        old=NL.join(["    docs_url=None,", "    redoc_url=None,", "    openapi_url=None,"]),
        new=NL.join(['    docs_url="/docs",', "    redoc_url=None,", '    openapi_url="/openapi.json",']),
        extra_args=("--any-case",),
    ),
    Break(
        "csrf-disabled", "the CSRF check never runs",
        ("tests/test_auth_session.py::test_a_cookie_write_without_the_csrf_token_is_refused",),
        file="kurukuru/auth.py",
        old='if principal.via == "session" and method in _UNSAFE_METHODS:',
        new='if False and principal.via == "session" and method in _UNSAFE_METHODS:',
    ),
    Break(
        "spa-swallows-api-404", "an unknown /api path is served the dashboard",
        ("tests/test_dashboard.py::test_an_unknown_api_path_is_a_json_404_not_the_dashboard",),
        file="kurukuru/dashboard.py",
        old='return JSONResponse({"detail": "Not Found"}, status_code=404)',
        new="pass",
    ),
    Break(
        "index-cached", "index.html cached as immutable",
        ("tests/test_dashboard.py::test_the_index_is_never_cached",),
        file="kurukuru/dashboard.py",
        old='return FileResponse(index, headers={"Cache-Control": _HTML_CACHE_CONTROL})',
        new='return FileResponse(index, headers={"Cache-Control": _IMMUTABLE_CACHE_CONTROL})',
    ),
)


def _outcomes(junit: Path) -> dict[str, list[bool]]:
    """``module::function`` -> one entry per case, True where the case failed."""
    results: dict[str, list[bool]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        module = case.get("classname", "").replace(".", "/") + ".py"
        module = module.split("backend/", 1)[-1]
        name = case.get("name", "").split("[", 1)[0]
        failed = case.find("failure") is not None or case.find("error") is not None
        if case.find("skipped") is not None:
            continue
        results.setdefault(f"{module}::{name}", []).append(failed)
    return results


#: Written beside a file before a break edits it, removed after the restore.
#: A ``finally`` does not run when the process is killed outright — the first
#: version of this tool, stopped mid-run, left ``console.py`` dropping the RFB
#: greeting's first byte — so the backup is what makes a killed run recoverable.
BACKUP_SUFFIX = ".prove-defences-orig"


def recover_interrupted_runs() -> list[Path]:
    """Put back any file a previous, killed run left broken."""
    restored = []
    for backup in BACKEND.rglob(f"*{BACKUP_SUFFIX}"):
        target = backup.with_name(backup.name[: -len(BACKUP_SUFFIX)])
        target.write_bytes(backup.read_bytes())
        backup.unlink()
        restored.append(target)
    return restored


def _apply(brk: Break) -> tuple[Path, bytes] | None:
    path = BACKEND / brk.file
    original = path.read_bytes()
    text = original.decode("utf-8")
    old, new = brk.old, brk.new
    if "\r\n" in text:
        old, new = old.replace(NL, "\r\n"), new.replace(NL, "\r\n")
    count = text.count(old)
    if count != 1:
        raise SystemExit(
            f"{brk.key}: anchor found {count} times in {brk.file}. The code it "
            f"breaks has moved; update the anchor rather than deleting the break."
        )
    path.with_name(path.name + BACKUP_SUFFIX).write_bytes(original)
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    return path, original


def run(brk: Break) -> list[str]:
    """Apply one break, run its tests, restore. Returns problems; empty means proven."""
    any_case = "--any-case" in brk.extra_args
    # The named tests only, not their whole modules: a break should be judged
    # on what it claims to break. Running a module's other tests under, say, a
    # shadowed Starlette 0.46.2 hung the first version of this tool on a
    # WebSocket test that break had nothing to do with.
    targets = list(brk.must_fail)
    env = dict(os.environ)
    restore = None
    with tempfile.TemporaryDirectory() as tmp:
        junit = Path(tmp) / "junit.xml"
        if brk.shadow_package:
            shadow = Path(tmp) / "shadow"
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                 "--target", str(shadow), brk.shadow_package],
                check=True,
            )
            env["PYTHONPATH"] = os.pathsep.join([str(shadow), env.get("PYTHONPATH", "")])
        try:
            if brk.file:
                restore = _apply(brk)
            try:
                subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     f"--junitxml={junit}", *targets],
                    cwd=BACKEND, env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=BREAK_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return [f"timed out after {BREAK_TIMEOUT_SECONDS}s - a hang is not a failure"]
        finally:
            if restore is not None:
                path, original = restore
                path.write_bytes(original)
                path.with_name(path.name + BACKUP_SUFFIX).unlink()
        outcomes = _outcomes(junit) if junit.exists() else {}

    problems = []
    for name in brk.must_fail:
        cases = outcomes.get(name)
        if not cases:
            problems.append(f"{name}: never ran - renamed, deleted or skipped?")
        elif not (any(cases) if any_case else all(cases)):
            survived = cases.count(False)
            problems.append(f"{name}: PASSED with the defence broken "
                            f"({survived} of {len(cases)} case(s) survived)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="*", default=[], help="break keys (substring match)")
    parser.add_argument("--list", action="store_true", help="list the breaks and exit")
    args = parser.parse_args(argv)

    for target in recover_interrupted_runs():
        print(f"RESTORED  {target.relative_to(REPO)} - a previous run was killed "
              f"with a break applied", flush=True)

    selected = [b for b in BREAKS if not args.only or any(o in b.key for o in args.only)]
    if args.list:
        for b in selected:
            print(f"{b.key:24} {b.what}")
        return 0

    failures = 0
    for brk in selected:
        problems = run(brk)
        print(f"{'PROVEN' if not problems else 'UNPROVEN':9} {brk.key:24} {brk.what}", flush=True)
        for p in problems:
            print(f"          {p}")
        failures += bool(problems)
    print(f"\n{len(selected) - failures} of {len(selected)} breaks proven.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
