"""
The licence is a shipped artefact, so it is checked like one.

Two things can go quietly wrong with licensing, and both are the kind of wrong
nobody notices until it matters legally rather than technically:

* the copy that ships inside the package drifts from the one in the repository,
  so the published artefact carries different terms from the source;
* the SPDX identifier in the metadata stops matching the text of the file.

Neither produces a failing build or a bug report. So they are asserted.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent

#: Duplicated rather than referenced, because setuptools resolves
#: ``license-files`` relative to the directory holding ``pyproject.toml`` and
#: has no way to reach a parent. The repository root holds the canonical copy —
#: it is what GitHub renders and what a reader looks for — and ``backend/`` holds
#: the one that goes into the wheel. This test is what keeps them one document.
DUPLICATED = ("LICENSE", "NOTICE")


@pytest.mark.parametrize("name", DUPLICATED)
def test_the_packaged_copy_matches_the_repository_copy(name: str):
    canonical = (REPO / name).read_bytes()
    packaged = (BACKEND / name).read_bytes()

    assert packaged == canonical, (
        f"backend/{name} has drifted from {name} at the repository root. "
        f"They are one document; copy the root one over the other."
    )


def test_the_licence_is_apache_2_0_in_metadata_and_in_text():
    metadata = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    text = (REPO / "LICENSE").read_text(encoding="utf-8")

    assert metadata["project"]["license"] == "Apache-2.0"
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text


def test_the_licence_carries_a_copyright_line():
    """The appendix boilerplate is meant to be filled in, and an unfilled one
    is a licence that names no owner."""
    text = (REPO / "LICENSE").read_text(encoding="utf-8")

    assert "[yyyy]" not in text and "[name of copyright owner]" not in text
    assert "Copyright 2026 Paul Nwokolo" in text


def test_the_apache_terms_are_unmodified():
    """Only the copyright line may differ from the canonical text.

    Checked structurally rather than by checksum so this does not need network
    access: the section headings and the clause count are what would move if
    someone edited the terms, and editing the terms of a standard licence while
    still calling it Apache-2.0 is the failure worth catching.
    """
    text = (REPO / "LICENSE").read_text(encoding="utf-8")

    for clause in (
        "1. Definitions.",
        "2. Grant of Copyright License.",
        "3. Grant of Patent License.",
        "4. Redistribution.",
        "5. Submission of Contributions.",
        "6. Trademarks.",
        "7. Disclaimer of Warranty.",
        "8. Limitation of Liability.",
        "9. Accepting Warranty or Additional Liability.",
        "END OF TERMS AND CONDITIONS",
    ):
        assert clause in text, f"missing from LICENSE: {clause}"


def test_qemu_is_declared_as_bundled_and_separately_licensed():
    """The one obligation bundling actually creates.

    QEMU is GPLv2 and invoked as a separate process, so it does not reach this
    code — but distributing its binaries means shipping its licence and offering
    its source, and a notices file that forgot to say so would be the whole
    problem.
    """
    notices = (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert "GNU General Public License, version 2" in notices
    assert "Written offer of source code" in notices
    assert "download.qemu.org/qemu-11.1.0.tar.xz" in notices
    # And the NOTICE has to point at it, or nobody reading only that file learns
    # there is a second licence involved at all.
    assert "THIRD-PARTY-NOTICES.md" in (REPO / "NOTICE").read_text(encoding="utf-8")


def test_the_pinned_qemu_version_matches_the_tested_range():
    """The notices name a version; the code records one. They must agree.

    Recording a version the installer does not ship would make the dashboard's
    "tested" badge a statement about a different binary.
    """
    from kurukuru.config import Settings

    notices = (REPO / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")

    assert f"**Pinned version** | {Settings().qemu_version_max_tested}" in notices


def test_pyinstaller_is_a_declared_build_dependency():
    """It was installed by hand for a prototype once. Undeclared toolchain is
    how a build stops being reproducible by anyone but its author."""
    metadata = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    build = metadata["project"]["optional-dependencies"]["build"]

    assert any(dep.startswith("pyinstaller==") for dep in build), build
