"""Tests for the alternating-runs harness.

Nothing here boots a VM. What is worth pinning is the reporting contract — that
the tool refuses to compare thin arms, and that it never emits a verdict — because
that refusal is the entire reason the tool exists (DECISIONS #40).

    python -m pytest tools/test_ab_measure.py -q
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from ab_measure import MIN_RUNS, parse_arm, summarise


def result(arm: str, index: int, reached: float | None) -> dict:
    return {
        "arm": arm,
        "index": index,
        "reached_s": reached,
        "last_colours": 853 if reached else 31,
        "last_dominant": "180052" if reached else "000000",
        "free_gb_before": 3.5,
    }


def test_thin_arms_are_refused_a_comparison():
    """Two runs per arm is exactly the trap Phase 13 fell into three times."""
    results = [result("a", 0, 21.0), result("b", 1, None),
               result("a", 2, 21.0), result("b", 3, None)]
    out = summarise(results, ["a", "b"])
    assert "NO COMPARISON OFFERED" in out
    assert f"fewer than {MIN_RUNS} runs" in out


def test_three_runs_each_gets_a_distribution_but_still_no_verdict():
    results = []
    for i in range(3):
        results.append(result("a", i * 2, 21.0))
        results.append(result("b", i * 2 + 1, None))
    out = summarise(results, ["a", "b"])
    assert "NO COMPARISON OFFERED" not in out
    assert "This tool does not conclude" in out
    # A distribution, not a winner.
    assert "reached 3/3" in out and "reached 0/3" in out
    for banned in ("faster", "slower", "cause", "confirmed", "proves"):
        assert banned not in out.lower()


def test_a_mixed_arm_is_visible_rather_than_averaged_away():
    """The 3/3-then-both-variants-pass episode: a streak must not read as a
    property, so the per-arm line has to show the split."""
    results = [result("a", 0, 21.0), result("b", 1, None),
               result("a", 2, None), result("b", 3, None),
               result("a", 4, 21.0), result("b", 5, 22.0)]
    out = summarise(results, ["a", "b"])
    assert "reached 2/3" in out          # arm a is mixed and says so
    assert "reached 1/3" in out          # so is arm b


def test_per_run_table_preserves_execution_order():
    """Alternation is only auditable if the report keeps the order runs happened
    in — a failure means something different when bracketed by successes."""
    results = [result("a", 0, 21.0), result("b", 1, None), result("a", 2, 21.0)]
    out = summarise(results, ["a", "b"])
    body = out.split("PER-ARM")[0]
    assert body.index("  1  a") < body.index("  2  b") < body.index("  3  a")


def test_distribution_reports_min_median_max():
    results = [result("a", 0, 10.0), result("a", 1, 20.0), result("a", 2, 60.0),
               result("b", 3, None), result("b", 4, None), result("b", 5, None)]
    out = summarise(results, ["a", "b"])
    assert "min=10.0s" in out and "median=20.0s" in out and "max=60.0s" in out


def test_arm_spec_parsing():
    assert parse_arm("novnc:") == ("novnc", [])
    assert parse_arm("vnc:-vnc;127.0.0.1:30") == ("vnc", ["-vnc", "127.0.0.1:30"])
    # A single QEMU argv token can itself contain commas (`-device` properties);
    # the delimiter is `;` precisely so this is not split apart.
    assert parse_arm("usb:-device;qemu-xhci,id=xhci") == (
        "usb", ["-device", "qemu-xhci,id=xhci"])
