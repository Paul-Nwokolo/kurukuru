"""
Say what a test run skipped, and refuse a run that skipped what it is for.

    python tools/report_skips.py junit.xml --must-run tests/test_process.py::test_x ...

A green run is a claim about the tests that ran. On a platform the suite was
not written on, the tests that matter most are exactly the ones most likely to
skip quietly: the POSIX-only ones, and the ones that skip when a binary is
missing. So this:

* prints every skipped test with its reason, as a GitHub annotation when run in
  Actions, so skips are read rather than counted;
* fails if any test named with ``--must-run`` was skipped or **did not exist**
  — a renamed test must fail this loudly rather than drop out of the list;
* fails if nothing ran at all.

It is not an allowlist of acceptable skips. Unexpected skips are reported, not
judged: whether a new skip is fine is a reading job, which is why it is
printed where it will be read.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET


def _cases(junit: str):
    for case in ET.parse(junit).getroot().iter("testcase"):
        module = case.get("classname", "").replace(".", "/") + ".py"
        name = case.get("name", "")
        skipped = case.find("skipped")
        failed = case.find("failure") is not None or case.find("error") is not None
        reason = skipped.get("message", "") if skipped is not None else ""
        yield f"{module}::{name}", skipped is not None, failed, reason


def report(junit: str, must_run: list[str], *, annotate: bool) -> int:
    cases = list(_cases(junit))
    ran = [c for c in cases if not c[1]]
    skipped = [c for c in cases if c[1]]

    print(f"{len(ran)} ran, {len(skipped)} skipped")
    for test, _, _, reason in skipped:
        line = f"SKIPPED {test}: {reason}"
        print(f"::notice title=skipped::{line}" if annotate else line)

    problems = []
    if not ran:
        problems.append("no test ran at all")
    for wanted in must_run:
        matches = [c for c in cases if c[0] == wanted or c[0].startswith(wanted + "[")]
        if not matches:
            problems.append(f"{wanted} does not exist - renamed or deleted? Update the list.")
        elif all(c[1] for c in matches):
            problems.append(f"{wanted} was skipped: {matches[0][3]}")
    for problem in problems:
        print(f"::error title=skips::{problem}" if annotate else f"ERROR {problem}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("junit")
    parser.add_argument("--must-run", nargs="*", default=[])
    args = parser.parse_args(argv)
    return report(args.junit, args.must_run, annotate=bool(os.environ.get("GITHUB_ACTIONS")))


if __name__ == "__main__":
    sys.exit(main())
