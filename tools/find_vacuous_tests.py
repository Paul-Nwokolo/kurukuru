"""
Find tests a *failure* would also satisfy.

Run deliberately, like ``ab_measure.py`` — not as a gate. It reports a shape,
not a verdict, and some of what it reports is correct code: a test genuinely
about absence looks identical to a test that passes because nothing happened.
Wiring it into CI would mean an allowlist, and an allowlist of "known
acceptable" findings is the hand-maintained list this project has been bitten
by twice.

**The shape it looks for** is the intersection of four properties in one test:

1. it makes an HTTP call,
2. it never asserts a status code,
3. it never subscripts a decoded body, and
4. every one of its assertions is satisfied by nothing having happened —
   ``not in``, ``is None``, ``== 0``, ``== []``, ``is False``, ``not x``.

Any one of those alone is usually fine, and it is the overlap that lies:
nothing left in such a test distinguishes "the system did the right thing" from
"the request never worked".

The third is the one that makes the output worth reading. An error response
still decodes, so ``response.json()["id"]`` raises ``KeyError`` on a 500 and the
test *errors* rather than passing — a test that reads a field out of the body
cannot pass by failing, however negative its assertions look. Leaving that out
reported 15 tests, of which 7 were fine for exactly this reason.

**Why it exists.** ``test_a_traversal_over_http_falls_through_to_the_app``
asserted only that a secret string was absent from the response body. A 500, a
404, an empty body and a request that was never sent all satisfy that, and it
was green for months while proving nothing. Finding the second instance took
this script; the second instance turned out to be a *helper* —
``test_networks._add`` returned its response unchecked, so every test built on
it inherited the hole.

**A note on what it does not catch.** It reads assertions, not intent. A test
that asserts a status and then checks the wrong field passes this and is still
wrong. Nothing here replaces reading the test.

Usage:
    python tools/find_vacuous_tests.py
    python tools/find_vacuous_tests.py --path backend/tests --quiet
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

#: Methods that look like an HTTP call on a client object.
HTTP_VERBS = frozenset(
    {"get", "post", "put", "patch", "delete", "request", "head", "options"}
)

#: Calls that assert a status internally, so the test does not have to.
STATUS_ASSERTING_CALLS = frozenset({"raise_for_status", "assert_ok"})


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.path.as_posix()}:{self.line}  {self.name}"


def _calls_http(node: ast.AST) -> bool:
    return any(
        isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Attribute)
        and sub.func.attr in HTTP_VERBS
        for sub in ast.walk(node)
    )


def _asserts_status(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "status_code":
            return True
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr in STATUS_ASSERTING_CALLS
        ):
            return True
    return False


def _indexes_a_decoded_body(node: ast.AST) -> bool:
    """True if the test subscripts a ``.json()`` result somewhere.

    This is the difference between a finding and noise, and it is a fact about
    Python rather than a heuristic: an error response still decodes, but it
    decodes to something like ``{"detail": ...}``, so ``response.json()["id"]``
    raises ``KeyError`` and the test *errors* instead of passing. A test that
    reads a field out of the body therefore cannot pass by failing, however
    negative its assertions look.

    Only subscripting counts. ``json.dumps(body)`` and ``len(body)`` are happy
    with an error body and prove nothing.
    """
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Subscript):
            continue
        for inner in ast.walk(sub.value):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "json"
            ):
                return True
    return False


def is_negative(test: ast.AST) -> bool:
    """True when "nothing happened" satisfies this assertion.

    ``is not None`` and ``!= x`` are **positive** — they claim something exists.
    Counting them as negative was this script's own first bug, and it made the
    output 214 lines long and unreadable, which is the failure mode of a
    diagnostic nobody ends up running.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return True
    if not isinstance(test, ast.Compare):
        return False
    if any(isinstance(op, ast.NotIn) for op in test.ops):
        return True
    for op, comparator in zip(test.ops, test.comparators):
        if not isinstance(op, (ast.Eq, ast.Is)):
            continue
        if isinstance(comparator, ast.Constant) and comparator.value in (0, None, False, ""):
            return True
        if isinstance(comparator, (ast.List, ast.Tuple)) and not comparator.elts:
            return True
        if isinstance(comparator, ast.Dict) and not comparator.keys:
            return True
    return False


def scan_source(source: str, path: Path) -> list[Finding]:
    """Every test in ``source`` matching all three properties."""
    findings: list[Finding] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        if not asserts:
            continue
        if (
            _calls_http(node)
            and not _asserts_status(node)
            and not _indexes_a_decoded_body(node)
            and all(is_negative(a.test) for a in asserts)
        ):
            findings.append(Finding(path, node.lineno, node.name))
    return findings


def scan(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("test_*.py")):
        findings.extend(scan_source(path.read_text(encoding="utf-8"), path))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default="backend/tests",
        help="Directory to scan (default: backend/tests).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Print only the findings, for piping.",
    )
    args = parser.parse_args()

    root = Path(args.path)
    if not root.is_dir():
        print(f"No such directory: {root}", file=sys.stderr)
        return 2

    findings = scan(root)

    if not args.quiet:
        print(f"Scanning {root.as_posix()} for tests a failure would also satisfy.\n")
    for finding in findings:
        print(f"  {finding}")
    if not args.quiet:
        print(
            f"\n{len(findings)} to read. Each needs judgement: a test about "
            "absence looks the same from here. What distinguishes them is "
            "whether anything in the test would notice the request failing —\n"
            "  indexing response.json()[...] would raise, and is fine;\n"
            "  asserting only on response.text would not, and is not."
        )
    # Always 0. This reports, it does not gate — see the module docstring.
    return 0


if __name__ == "__main__":
    sys.exit(main())
