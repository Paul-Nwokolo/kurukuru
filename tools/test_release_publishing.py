"""The PyPI publish job stays gated, tokenless, loud when off, and rehearsable.

Publishing to PyPI cannot be undone: a version, once uploaded, is spent
forever, and the first upload is what claims the name. So the job in
release.yml has exactly two ways in — a real release the maintainer has
switched on, and a TestPyPI rehearsal that cannot reach real PyPI — it
authenticates by trusted publishing, never a stored token, and a release that
skips PyPI says so instead of reading as finished. These tests read the
workflow and fail if any of that is edited away. DECISIONS #74.
"""

from __future__ import annotations

from pathlib import Path

import yaml

RELEASE = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"

REAL = "(startsWith(github.ref,'refs/tags/v')&&github.event_name=='push'&&vars.PYPI_PUBLISHING=='enabled')"
REHEARSAL = "(github.event_name=='workflow_dispatch'&&inputs.rehearse_testpypi)"


def _workflow() -> dict:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def _jobs() -> dict:
    return _workflow()["jobs"]


def _squash(text: str) -> str:
    return "".join(str(text).split())


def test_publishing_has_exactly_two_ways_in():
    # The real release, switched on by the maintainer; or the rehearsal. Each
    # clause complete, and nothing else that could open the job.
    clauses = _squash(_jobs()["pypi"]["if"]).split("||")
    assert sorted(clauses) == sorted([REAL, REHEARSAL])


def test_the_rehearsal_can_never_target_real_pypi():
    """Index, environment and project page are all chosen by one test —
    "is this a tag push?" — and a dispatch is never one. Real PyPI's publisher
    accepts only the `pypi` environment, which deploys only v* tags, so even
    a broken expression here could not upload a rehearsal to it."""
    job = _jobs()["pypi"]
    assert _squash(job["environment"]["name"]) == \
        "${{github.event_name=='push'&&'pypi'||'testpypi'}}"
    publish = next(s for s in job["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses", "")))
    assert _squash(publish["with"]["repository-url"]) == \
        "${{github.event_name=='push'&&'https://upload.pypi.org/legacy/'||'https://test.pypi.org/legacy/'}}"
    rehearse = _workflow()[True]["workflow_dispatch"]["inputs"]["rehearse_testpypi"]
    assert rehearse["type"] == "boolean" and rehearse["default"] is False
    # And the rehearsal publishes a .dev version, so it never spends a real one.
    script = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert '.dev${GITHUB_RUN_ID}' in script


def test_publishing_waits_for_the_rest_of_the_release():
    assert set(_jobs()["pypi"]["needs"]) >= {"installer", "checksum"}


def test_publishing_uses_trusted_publishing_and_no_stored_token():
    job = _jobs()["pypi"]
    assert job["permissions"].get("id-token") == "write"

    publish = [s for s in job["steps"] if "pypa/gh-action-pypi-publish" in str(s.get("uses", ""))]
    assert len(publish) == 1, "exactly one publish step, using the official action"
    assert not {"password", "user"} & set(publish[0].get("with") or {}), (
        "a password/user input means a stored token; trusted publishing needs neither"
    )
    # skip-existing would turn a second upload of a spent version into a pass.
    assert "skip-existing" not in (publish[0].get("with") or {})
    assert "secrets." not in yaml.safe_dump(job), "no secret may reach the publish job"


def test_a_release_without_pypi_fails_loudly():
    """The probe showed a skip reads as a finished release. The gate job is
    the exact complement of the real way in, and fails the run."""
    gate = _jobs()["pypi-gate"]
    assert _squash(gate["if"]) == REAL.strip("()").replace("=='enabled'", "!='enabled'")
    script = "\n".join(str(s.get("run", "")) for s in gate["steps"])
    assert "::error" in script and "exit 1" in script
    assert "environment-level" in script, "must name the trap the probe measured"


def test_the_installed_package_is_checked_after_every_publish():
    verify = _jobs()["pypi-verify"]
    assert verify["needs"] == "pypi" or "pypi" in verify["needs"]
    # The step scripts as written — not re-serialised YAML, which escapes the
    # quotes in meta["Version"] and made this check fail on a correct file.
    script = "\n".join(str(step.get("run", "")) for step in verify["steps"])
    env = {k: v for step in verify["steps"] for k, v in (step.get("env") or {}).items()}
    # The repository check itself, not just "Project-URL": the job reads the
    # Project-URLs in more than one place, and deleting only the assertion on
    # our repository left "Project-URL" in the script and this test passing.
    assert env.get("OUR_REPOSITORY") == "https://github.com/Paul-Nwokolo/kurukuru"
    for must in ("pipx install", "NOT OUR PACKAGE", "BUILT_SHA256", 'meta["Version"]',
                 "https://pypi.org/simple/", "https://test.pypi.org/simple/"):
        assert must in script, f"pypi-verify no longer checks {must}"
    # Each check prints its actual value, so the log is the audit trail: the
    # hashes, name and version, and which Project-URL matched ours.
    for printed in ('echo "built:  $BUILT_SHA256"', 'echo "served: $served"',
                    'print("Name:", meta["Name"], "| Version:", meta["Version"])',
                    'print(f"Repository check: expected {ours}; matched by {matched}")'):
        assert printed in script, f"pypi-verify no longer prints {printed}"


def test_propagation_is_retried_bounded_and_named():
    # PyPI's CDN lags an upload; a release must not look broken for that, and
    # must not wait forever either (docs/RELEASING.md, "Propagation").
    script = "\n".join(str(s.get("run", "")) for s in _jobs()["pypi-verify"]["steps"])
    assert "for i in $(seq 1 30)" in script, "the index wait must be bounded"
    assert "title=pypi propagation" in script, "running out must be reported as propagation"
