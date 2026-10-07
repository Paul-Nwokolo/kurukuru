"""Every defence test has a break that proves it.

``prove_defences.BREAKS`` is a hand-kept list, and a hand-kept list goes stale
precisely when someone adds the thing it should have listed. So this reads the
defence module's test functions straight from its source and fails if any of
them is not named in some break's ``must_fail`` — a new defence test cannot
land unproven. It does not run the breaks (that is ``prove_defences.py``'s job,
and CI runs it); it makes sure there is nothing to forget.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import prove_defences  # noqa: E402

DEFENCE_MODULE = prove_defences.BACKEND / prove_defences.DEFENCES


def _defence_tests() -> set[str]:
    tree = ast.parse(DEFENCE_MODULE.read_text(encoding="utf-8"))
    return {
        f"{prove_defences.DEFENCES}::{node.name}"
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def _proven() -> set[str]:
    return {name for brk in prove_defences.BREAKS for name in brk.must_fail}


def test_the_defence_module_has_tests():
    """Guards the guard: an empty set would make the check below vacuous."""
    assert len(_defence_tests()) >= 8


def test_every_defence_test_has_a_break_that_must_fail_it():
    unproven = sorted(_defence_tests() - _proven())
    assert not unproven, (
        "These defence tests have no deliberate break proving they fail when "
        "the defence is gone. Add one to tools/prove_defences.py and run it: "
        + ", ".join(unproven)
    )


def test_every_break_names_tests_that_exist():
    """A rename would otherwise leave a break proving a test that is gone —
    reported by the runner as 'never ran', but cheaper to catch here."""
    known = _defence_tests()
    stale = sorted(
        name for name in _proven()
        if name.startswith(prove_defences.DEFENCES) and name not in known
    )
    assert not stale, f"Breaks name defence tests that no longer exist: {stale}"


def test_every_break_does_one_thing():
    for brk in prove_defences.BREAKS:
        assert brk.must_fail, f"{brk.key} names no test it must fail"
        assert (brk.file is None) != (brk.shadow_package is None), (
            f"{brk.key} must either edit a file or shadow a package, not both or neither"
        )


def test_no_break_is_left_applied():
    """No break's text is present in the real source.

    The tool now edits a throwaway copy, so it cannot leave a break behind —
    but an early version edited the tree in place and, killed mid-run, did
    exactly that to ``console.py``. Cheap to keep: if anything ever puts a
    break's text back into the real tree, by hand or by a regression in the
    tool, the suite says so before it can be committed."""
    applied = []
    for brk in prove_defences.BREAKS:
        if brk.file is None:
            continue
        raw = (prove_defences.BACKEND / brk.file).read_bytes().decode("utf-8")
        if brk.new in raw.replace("\r\n", "\n"):
            applied.append(f"{brk.key} in {brk.file}")
    assert not applied, f"Deliberate breaks are present in the real source: {applied}"
