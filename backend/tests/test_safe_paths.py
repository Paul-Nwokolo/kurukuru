"""Tests for the shared path-containment helper.

The interesting cases are not "does it reject ``..``" — every version of this
code got that right. They are the two things the previous version got wrong:

* a **rooted** right-hand side, which replaces the base rather than extending
  it, so the join is not under the base at all; and
* **when** the filesystem is consulted, because on Windows resolving a UNC path
  is a network operation, and doing it before the containment check turns a
  path-traversal attempt into an outbound connection to a host the caller chose.

The second is asserted directly: ``resolve`` is patched to record every call, and
a UNC input must produce *no* call at all. Asserting only on the return value
would pass against the buggy version, which also returned None — after dialling
out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kurukuru.safe_paths import resolve_within


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "assets").mkdir(parents=True)
    (root / "assets" / "app.js").write_text("ok", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    return root


def test_an_ordinary_asset_resolves(tree: Path) -> None:
    found = resolve_within(tree, "assets/app.js")
    assert found is not None
    assert found.read_text(encoding="utf-8") == "ok"


def test_dot_dot_is_refused(tree: Path) -> None:
    assert resolve_within(tree, "../outside.txt") is None
    assert resolve_within(tree, "assets/../../outside.txt") is None


def test_an_absolute_path_is_refused(tree: Path, tmp_path: Path) -> None:
    """The join discards the base entirely, which is the whole problem."""
    assert resolve_within(tree, str(tmp_path / "outside.txt")) is None


def test_a_missing_file_inside_the_root_still_resolves(tree: Path) -> None:
    """Containment is the question here; existence is the caller's."""
    found = resolve_within(tree, "assets/not-there.js")
    assert found is not None
    assert not found.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="UNC paths are a Windows shape")
@pytest.mark.parametrize(
    "attempt",
    [
        "//evil.invalid/share/x",
        "\\\\evil.invalid\\share\\x",
        "//./C:/Windows/win.ini",
        "//?/UNC/evil.invalid/share",
    ],
)
def test_a_unc_path_is_refused_without_touching_the_network(
    tree: Path, attempt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal must happen *before* anything resolves the path.

    ``.invalid`` is reserved and cannot resolve, so even a regression cannot
    reach a real host from this test — but the assertion is on the call, not on
    the network, because a machine with a DNS wildcard would make the weaker
    assertion pass while the bug was present.
    """
    calls: list[str] = []
    real_resolve = Path.resolve

    def recording_resolve(self: Path, *args: object, **kwargs: object) -> Path:
        calls.append(str(self))
        return real_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", recording_resolve)

    assert resolve_within(tree, attempt) is None

    # The base is resolved on the way in, and that is ours. Nothing that looks
    # like the attacker's path may be.
    assert not any("evil.invalid" in call for call in calls), (
        f"resolve() was called on the attacker-supplied path: {calls}"
    )
    assert not any(call.startswith("\\\\") for call in calls), (
        f"resolve() was called on a UNC path: {calls}"
    )
