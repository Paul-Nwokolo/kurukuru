"""Tests for the vacuous-test finder.

The detector is worth testing because both of its bugs were in the *judgement*
rather than the plumbing, and neither showed up as an error — one made it flag
correct code, the other made it miss nothing but drown the real findings in 200
lines of noise. A diagnostic that cries wolf is a diagnostic nobody runs, which
is the same failure as one that reports nothing.

Each case below is a real shape from this repository's own history rather than
an invented one.
"""

from __future__ import annotations

from pathlib import Path

from find_vacuous_tests import scan_source

HERE = Path("fake_tests.py")


def found(source: str) -> list[str]:
    return [f.name for f in scan_source(source, HERE)]


# --------------------------------------------------------------------------- #
# The shape it exists to find
# --------------------------------------------------------------------------- #
def test_the_original_traversal_test_is_flagged() -> None:
    """Verbatim shape of the test that was green while proving nothing."""
    assert found(
        '''
def test_a_traversal_over_http_falls_through_to_the_app(served, built):
    for attempt in ("/../secret.txt", "/..%2fsecret.txt"):
        assert "do not serve me" not in served.get(attempt).text
'''
    ) == ["test_a_traversal_over_http_falls_through_to_the_app"]


def test_an_empty_collection_after_an_unchecked_call_is_flagged() -> None:
    """"The rows are gone" is equally true of rows never created."""
    assert found(
        '''
def test_terminating_drops_its_forwards(client):
    client.delete("/instances/abc")
    assert session.exec(select(PortForward)).all() == []
'''
    ) == ["test_terminating_drops_its_forwards"]


# --------------------------------------------------------------------------- #
# What it must not flag, or nobody will run it
# --------------------------------------------------------------------------- #
def test_a_status_assertion_clears_it() -> None:
    assert found(
        '''
def test_traversal(served):
    response = served.get("/../secret.txt")
    assert response.status_code == 200
    assert "do not serve me" not in response.text
'''
    ) == []


def test_indexing_the_response_clears_it() -> None:
    """A 500 body raises on the index, so the test cannot pass by failing."""
    assert found(
        '''
def test_degraded(client):
    assert client.get("/instances/abc").json()["degraded"] is False
'''
    ) == []


def test_is_not_none_is_a_positive_assertion() -> None:
    """The first version counted `is not` as negative and flagged this.

    It asserts that something *exists*. Reading it as "nothing happened
    satisfies this" is backwards, and it is why the first run was unreadable.
    """
    assert found(
        '''
def test_something(client):
    row = client.get("/instances/abc").json()
    assert row is not None
'''
    ) == []


def test_a_test_with_no_http_call_is_not_this_scripts_business() -> None:
    assert found(
        '''
def test_pure_unit():
    assert resolve_within(root, "../x") is None
'''
    ) == []


def test_raise_for_status_counts_as_asserting_the_status() -> None:
    assert found(
        '''
def test_uses_helper(client):
    response = client.get("/thing")
    response.raise_for_status()
    assert "secret" not in response.text
'''
    ) == []
