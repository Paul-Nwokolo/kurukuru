"""The PyPI publish job stays gated and tokenless.

Publishing to PyPI cannot be undone: a version, once uploaded, is spent
forever, and the first upload is what claims the name. So the job in
release.yml must not be able to fire until the maintainer has created the PyPI
account and the pending publisher and switched it on (docs/RELEASING.md), and
it must authenticate by trusted publishing, never a stored token. These tests
read the workflow and fail if either property is ever edited away.
DECISIONS #74.
"""

from __future__ import annotations

from pathlib import Path

import yaml

RELEASE = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


def _jobs() -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]


def test_publishing_is_gated_on_a_tag_and_the_maintainers_switch():
    condition = _jobs()["pypi"]["if"].replace(" ", "")
    assert "startsWith(github.ref,'refs/tags/v')" in condition
    assert "github.event_name=='push'" in condition
    assert "vars.PYPI_PUBLISHING=='enabled'" in condition
    # And all three are required, not alternatives.
    assert "||" not in condition


def test_publishing_waits_for_the_rest_of_the_release():
    assert set(_jobs()["pypi"]["needs"]) >= {"installer", "checksum"}


def test_publishing_uses_trusted_publishing_and_no_stored_token():
    job = _jobs()["pypi"]
    assert job["permissions"].get("id-token") == "write"
    assert job["environment"]["name"] == "pypi"

    publish = [s for s in job["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses", ""))]
    assert len(publish) == 1, "exactly one publish step, using the official action"
    assert not {"password", "user"} & set(publish[0].get("with") or {}), (
        "a password/user input means a stored token; trusted publishing needs neither"
    )
    assert "secrets." not in yaml.safe_dump(job), "no secret may reach the publish job"


def test_the_installed_package_is_checked_after_every_publish():
    verify = _jobs()["pypi-verify"]
    assert verify["needs"] == "pypi" or "pypi" in verify["needs"]
    # The step scripts as written — not re-serialised YAML, which escapes the
    # quotes in meta["Version"] and made this check fail on a correct file.
    script = "\n".join(str(step.get("run", "")) for step in verify["steps"])
    # The repository URL itself, not just "Project-URL": the job reads the
    # Project-URLs in more than one place, and deleting only the assertion on
    # our repository left "Project-URL" in the script and this test passing.
    for must in ("pipx install", "https://github.com/Paul-Nwokolo/kurukuru",
                 "BUILT_SHA256", 'meta["Version"]'):
        assert must in script, f"pypi-verify no longer checks {must}"
