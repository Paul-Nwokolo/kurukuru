"""
Creating the owner account over HTTP, once, from this machine.

Decision 47 put account creation on the host rather than over the API. Phase 16
keeps that boundary and drops a different one: *host-local* and *in a terminal*
are not the same requirement, and an installer's user has no terminal open.

So the route exists, and everything below is about the three things that make it
safe to exist: it only works while there is no account, it only accepts loopback
peers, and it enforces the same password rule as the terminal path.
"""

from __future__ import annotations

import pytest

from tests.test_instances_api import anon_client, client, iso_dir  # noqa: F401

SETUP = "/auth/first-run"
GOOD_PASSWORD = "a-long-enough-password"


def _fresh(anon_client):  # noqa: F811
    """The same client with every account removed — a genuinely fresh install.

    Against ``anon_client.db_engine``, which is the engine the application is
    actually wired to for this test. Reaching for ``kurukuru.database.engine``
    instead deletes from a database nothing is reading, and every assertion then
    fails against an install that still has its owner.
    """
    from sqlmodel import Session, select

    from kurukuru.models import ApiToken, ConsoleTicket, Session as SessionRow, User

    with Session(anon_client.db_engine) as db:  # type: ignore[attr-defined]
        for model in (ConsoleTicket, ApiToken, SessionRow, User):
            for row in db.exec(select(model)).all():
                db.delete(row)
        db.commit()
    anon_client.cookies.clear()
    return anon_client


def _from_peer(anon_client, peer: str):  # noqa: F811
    """A client over the same app whose requests arrive from ``peer``.

    The peer is fixed when the client is built — Starlette has no per-request
    hook for it — which is the honest shape anyway: it is the other end of the
    TCP connection, not something a caller can set per request.
    """
    from kurukuru.main import app

    from tests.conftest import api_client

    c = api_client(app, client=(peer, 40000))
    c.db_engine = anon_client.db_engine  # type: ignore[attr-defined]
    return c


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #
def test_a_fresh_install_reports_that_it_is_unconfigured(anon_client):  # noqa: F811
    body = _fresh(anon_client).get(SETUP).json()

    assert body["configured"] is False
    assert body["cli_name"]


def test_the_owner_can_be_created_from_loopback(anon_client):  # noqa: F811
    c = _fresh(anon_client)

    response = c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})

    assert response.status_code == 201, response.text
    assert response.json()["username"] == "owner"
    assert response.json()["is_owner"] is True
    assert c.get(SETUP).json()["configured"] is True


def test_creating_the_owner_signs_them_in(anon_client):  # noqa: F811
    """Otherwise the dashboard creates an account and then asks for the password
    typed ten seconds ago, which reads like it did not work."""
    from kurukuru import auth

    c = _fresh(anon_client)

    response = c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})

    assert auth.SESSION_COOKIE in response.cookies or auth.SESSION_COOKIE in c.cookies
    assert response.headers[auth.CSRF_HEADER]
    # And the session actually works.
    assert c.get("/auth/whoami").status_code == 200


def test_the_new_owner_can_sign_in_with_that_password(anon_client):  # noqa: F811
    c = _fresh(anon_client)
    c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})
    c.cookies.clear()

    assert c.post(
        "/auth/login", json={"username": "owner", "password": GOOD_PASSWORD}
    ).status_code == 200


# --------------------------------------------------------------------------- #
# It stops existing
# --------------------------------------------------------------------------- #
def test_a_second_call_is_refused(anon_client):  # noqa: F811
    """Not "requires authentication afterwards" — refused outright. A route that
    can create an owner is a route that can take an install over."""
    c = _fresh(anon_client)
    c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})

    response = c.post(SETUP, json={"username": "intruder", "password": GOOD_PASSWORD})

    assert response.status_code == 409
    assert "already has an account" in response.json()["detail"]


def test_it_cannot_overwrite_the_first_owners_password(anon_client):  # noqa: F811
    c = _fresh(anon_client)
    c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})
    c.cookies.clear()

    c.post(SETUP, json={"username": "owner", "password": "a-different-password"})

    # The original password still works and the new one does not.
    assert c.post(
        "/auth/login", json={"username": "owner", "password": "a-different-password"}
    ).status_code == 401
    assert c.post(
        "/auth/login", json={"username": "owner", "password": GOOD_PASSWORD}
    ).status_code == 200


def test_it_cannot_add_a_second_owner(anon_client):  # noqa: F811
    from sqlmodel import Session, select

    from kurukuru.models import User

    c = _fresh(anon_client)
    c.post(SETUP, json={"username": "owner", "password": GOOD_PASSWORD})
    c.post(SETUP, json={"username": "second", "password": GOOD_PASSWORD})

    with Session(c.db_engine) as db:  # type: ignore[attr-defined]
        assert len(db.exec(select(User)).all()) == 1


# --------------------------------------------------------------------------- #
# Only from this machine
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("peer", ["10.0.0.5", "192.168.1.20", "203.0.113.9"])
def test_a_remote_peer_is_refused(anon_client, peer):  # noqa: F811
    """The peer address is the other end of the TCP connection, not a header —
    a caller cannot assert it."""
    c = _fresh(anon_client)

    response = _from_peer(c, peer).post(
        SETUP,
        json={"username": "owner", "password": GOOD_PASSWORD},
        headers={"x-forwarded-for": "127.0.0.1"},  # forged, and ignored
    )

    assert response.status_code == 403
    assert "machine running" in response.json()["detail"]


def test_a_refused_remote_call_creates_nothing(anon_client):  # noqa: F811
    from sqlmodel import Session, select

    from kurukuru.models import User

    c = _fresh(anon_client)
    _from_peer(c, "10.0.0.5").post(
        SETUP, json={"username": "owner", "password": GOOD_PASSWORD}
    )

    with Session(c.db_engine) as db:  # type: ignore[attr-defined]
        assert db.exec(select(User)).first() is None


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.53", "::1"])
def test_loopback_peers_are_accepted(anon_client, peer):  # noqa: F811
    """The whole 127/8 block, not just 127.0.0.1, and IPv6 loopback too."""
    c = _fresh(anon_client)

    response = _from_peer(c, peer).post(
        SETUP, json={"username": "owner", "password": GOOD_PASSWORD}
    )

    assert response.status_code == 201, response.text


# --------------------------------------------------------------------------- #
# Same rules as the terminal path
# --------------------------------------------------------------------------- #
def test_a_short_password_is_refused(anon_client):  # noqa: F811
    """A minimum that applies in a terminal but not in a browser is no minimum."""
    from kurukuru.models import MIN_PASSWORD_LENGTH

    c = _fresh(anon_client)

    response = c.post(SETUP, json={"username": "owner", "password": "x" * (MIN_PASSWORD_LENGTH - 1)})

    assert response.status_code == 422
    assert c.get(SETUP).json()["configured"] is False
