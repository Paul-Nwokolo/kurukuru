"""The wheel check rejects every way a wheel can be the wrong artefact.

Proven once against a real build too: the backend wheeled without the
dashboard copy was refused for exactly the missing dashboard (Phase 18,
DECISIONS #71). These keep that true without a build in every test run.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_wheel  # noqa: E402

GOOD = [
    "kurukuru/__init__.py",
    "kurukuru/_dashboard/index.html",
    "kurukuru/_dashboard/assets/index-abc123.js",
    "kurukuru-0.1.4.dist-info/entry_points.txt",
    "kurukuru-0.1.4.dist-info/METADATA",
]


METADATA = (
    "Metadata-Version: 2.4\nName: kurukuru\nVersion: 0.1.4\n"
    "Project-URL: Homepage, https://github.com/Paul-Nwokolo/kurukuru\n"
)


def _wheel(tmp_path: Path, names: list[str], version: str = "0.1.4",
           metadata: str = METADATA) -> Path:
    path = tmp_path / f"kurukuru-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, metadata if name.endswith("/METADATA") else "x")
    return path


def test_a_complete_wheel_passes(tmp_path):
    assert build_wheel.check_wheel(_wheel(tmp_path, GOOD), "0.1.4") == []


def test_a_wheel_without_the_dashboard_is_refused(tmp_path):
    names = [n for n in GOOD if "_dashboard" not in n]
    problems = build_wheel.check_wheel(_wheel(tmp_path, names), "0.1.4")
    assert "the dashboard's index.html is not in the wheel" in problems
    assert "no hashed dashboard assets in the wheel" in problems


def test_tests_and_caches_must_not_ship(tmp_path):
    names = GOOD + ["kurukuru/__pycache__/main.cpython-313.pyc", "tests/test_cli.py"]
    problems = build_wheel.check_wheel(_wheel(tmp_path, names), "0.1.4")
    assert len([p for p in problems if p.startswith("should not ship")]) == 2


def test_the_wrong_version_is_refused(tmp_path):
    problems = build_wheel.check_wheel(_wheel(tmp_path, GOOD, version="0.1.3"), "0.1.4")
    assert any("is not version 0.1.4" in p for p in problems)


def test_no_entry_point_means_no_command(tmp_path):
    names = [n for n in GOOD if not n.endswith("entry_points.txt")]
    problems = build_wheel.check_wheel(_wheel(tmp_path, names), "0.1.4")
    assert any("entry_points.txt" in p for p in problems)


def test_a_wheel_that_does_not_name_this_repository_is_refused(tmp_path):
    # Refused before the upload, not only by pypi-verify after it (DECISIONS #74).
    for metadata in (
        "Name: kurukuru\nVersion: 0.1.4\n",
        "Name: kurukuru\nProject-URL: Homepage, https://github.com/someone-else/kurukuru\n",
        "Name: kurukuru\nProject-URL: Issues, https://github.com/Paul-Nwokolo/kurukuru/issues\n",
    ):
        problems = build_wheel.check_wheel(_wheel(tmp_path, GOOD, metadata=metadata), "0.1.4")
        assert any("no Project-URL naming" in p for p in problems), metadata
