"""The upgrade check rejects any install that is not exactly the build.

Each test builds a stage shaped like ``build_installer.py``'s and an install
directory from it the way the real ``kurukuru.iss`` lays it out — the expected
tree is read from that script, so these exercise the parser against the file
that ships, not a fixture copy of it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import verify_upgrade


@pytest.fixture()
def stage(tmp_path: Path) -> Path:
    root = tmp_path / "stage"
    files = {
        "dist/kurukuru/kurukuru.exe": b"MZ new exe",
        "dist/kurukuru/_internal/fastapi/_compat/__init__.py": b"# 0.120.4",
        "dist/kurukuru/_internal/fastapi-0.120.4.dist-info/METADATA": b"Version: 0.120.4",
        "dashboard/index.html": b"<!doctype html>",
        "dashboard/assets/index-new.js": b"new bundle",
        "qemu/qemu-system-x86_64.exe": b"MZ qemu",
        "LICENSE": b"Apache",
        "NOTICE": b"notice",
        "THIRD-PARTY-NOTICES.md": b"third party",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


@pytest.fixture()
def installed(tmp_path: Path, stage: Path) -> Path:
    """What a clean install of ``stage`` looks like on disk."""
    app = tmp_path / "app"
    for key, source in verify_upgrade.expected_tree(stage).items():
        target = app / key
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (app / "unins000.exe").write_bytes(b"inno")
    (app / "unins000.dat").write_bytes(b"inno log")
    return app


def _problems(app: Path, stage: Path) -> list[str]:
    return verify_upgrade.tree_problems(app, verify_upgrade.expected_tree(stage))


def test_the_expected_tree_comes_from_the_real_installer_script(stage: Path):
    """Guards the guard: if parsing [Files] broke, every comparison below would
    compare nothing against nothing and pass."""
    keys = set(verify_upgrade.expected_tree(stage))
    assert "kurukuru.exe" in keys
    assert "_internal/fastapi/_compat/__init__.py" in keys
    assert "dashboard/assets/index-new.js" in keys
    assert "qemu/qemu-system-x86_64.exe" in keys
    assert "startup-task.ps1" in keys  # sourced beside the script, not the stage


def test_a_clean_install_of_the_build_passes(installed: Path, stage: Path):
    assert _problems(installed, stage) == []


def test_the_previous_versions_leftovers_are_caught(installed: Path, stage: Path):
    """The 0.1.2 -> 0.1.4 shape exactly: the old FastAPI's metadata and the
    module it replaced, sitting beside the new ones."""
    old = installed / "_internal" / "fastapi-0.115.12.dist-info"
    old.mkdir()
    (old / "METADATA").write_bytes(b"Version: 0.115.12")
    (installed / "_internal" / "fastapi" / "_compat.py").write_bytes(b"# 0.115.12")

    problems = _problems(installed, stage)

    assert "not part of this build: _internal/fastapi-0.115.12.dist-info/metadata" in problems
    assert "not part of this build: _internal/fastapi/_compat.py" in problems


def test_a_file_with_the_old_contents_is_caught(installed: Path, stage: Path):
    """Same name, old bytes — invisible to any check that counts or lists."""
    (installed / "_internal" / "fastapi" / "_compat" / "__init__.py").write_bytes(b"# 0.115.12")

    assert _problems(installed, stage) == [
        "differs from the build: _internal/fastapi/_compat/__init__.py"
    ]


def test_a_missing_file_is_caught(installed: Path, stage: Path):
    (installed / "dashboard" / "assets" / "index-new.js").unlink()

    assert _problems(installed, stage) == ["missing: dashboard/assets/index-new.js"]


def test_a_stale_file_and_a_missing_one_do_not_cancel_out(installed: Path, stage: Path):
    """Why this is not a file count: one leftover plus one missing file keeps
    the count right and the install wrong."""
    (installed / "dashboard" / "assets" / "index-new.js").unlink()
    (installed / "dashboard" / "assets" / "index-old.js").write_bytes(b"old bundle")

    assert len(_problems(installed, stage)) == 2


def test_the_reported_framework_must_be_the_pinned_one():
    pinned = {"fastapi": "0.120.4", "starlette": "0.49.3"}

    assert verify_upgrade.framework_problems(dict(pinned), pinned) == []
    assert verify_upgrade.framework_problems(
        {"fastapi": "0.120.4", "starlette": "0.46.2"}, pinned
    ) == ["starlette: loaded '0.46.2', pinned '0.49.3'"]


def test_an_executable_that_cannot_say_what_it_loaded_is_not_this_build():
    """0.1.3's ``version --json`` has no ``framework`` at all. In the release
    workflow's negative control that absence is one of the reasons the
    previous release must fail this check."""
    problems = verify_upgrade.framework_problems(None, {"starlette": "0.49.3"})

    assert len(problems) == 1 and "does not report" in problems[0]


def test_the_pins_are_read_from_pyproject():
    pinned = verify_upgrade.pinned_framework()

    assert set(pinned) == {"fastapi", "starlette"}
