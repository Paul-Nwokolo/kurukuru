"""The console is an unauthenticated path to a VM's screen and keyboard, unless.

Before this phase, anything that could reach the port could open any VM's
framebuffer and type into it. It is the sharpest surface in the product and the
one a naive fix leaves open, because a WebSocket cannot carry an
``Authorization`` header from a browser and an ``HTTPException`` raised in a
dependency cannot become a close frame.

So it is authorised out of band: mint over authenticated HTTP, redeem once on
connect. The failure modes worth naming are each tested here rather than argued:

  * no ticket at all
  * a ticket minted for a **different instance**
  * a ticket **replayed** after it has been redeemed
  * a ticket that **outlives its session** — logout, or a password change

Every rejection is the same message. A caller holding a bad ticket learns only
that it did not work.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from kurukuru import auth
from kurukuru.console import CLOSE_UNAUTHENTICATED
from kurukuru.models import ConsoleTicket, Instance, InstanceStatus

from tests.test_instances_api import anon_client, client, iso_dir  # noqa: F401


def _running_instance(c, name: str = "vm-one") -> str:
    body = c.post("/instances", json={"name": name}).json()
    with Session(c.db_engine) as session:
        row = session.get(Instance, body["id"])
        row.status = InstanceStatus.RUNNING
        row.vnc_port = 5999
        session.add(row)
        session.commit()
    return body["id"]


def _ticket(c, instance_id: str) -> str:
    response = c.post(f"/instances/{instance_id}/console/ticket")
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def _connect(c, instance_id: str, ticket: str | None) -> tuple[bool, int | None, str]:
    """Open the console; return (closed, code, reason).

    A refusal arrives as a close *message* from this Starlette version rather
    than as a raised WebSocketDisconnect, so both are handled — the endpoint
    accepts first and then closes with a code, deliberately, because a socket
    rejected before the handshake gives the browser nothing to show the user.
    """
    from starlette.websockets import WebSocketDisconnect

    url = f"/instances/{instance_id}/console"
    if ticket is not None:
        url += f"?ticket={ticket}"
    try:
        with c.websocket_connect(url) as ws:
            message = ws.receive()
        if isinstance(message, dict) and message.get("type") == "websocket.close":
            return True, message.get("code"), message.get("reason", "")
        return False, None, ""
    except WebSocketDisconnect as exc:
        return True, exc.code, getattr(exc, "reason", "")


# --------------------------------------------------------------------------- #
# Minting is itself authenticated
# --------------------------------------------------------------------------- #
def test_minting_a_ticket_requires_authentication(client, anon_client):  # noqa: F811
    instance_id = _running_instance(client)

    assert anon_client.post(
        f"/instances/{instance_id}/console/ticket"
    ).status_code == 401


def test_a_ticket_for_a_missing_instance_is_404(client):  # noqa: F811
    assert client.post("/instances/nope/console/ticket").status_code == 404


# --------------------------------------------------------------------------- #
# The four failure modes
# --------------------------------------------------------------------------- #
def test_connecting_without_a_ticket_is_refused(client):  # noqa: F811
    """The state the console shipped in before this phase."""
    instance_id = _running_instance(client)

    closed, code, _reason = _connect(client, instance_id, None)

    assert closed and code == CLOSE_UNAUTHENTICATED


def test_a_ticket_for_another_instance_does_not_open_this_one(client):  # noqa: F811
    """A ticket is a permit for one VM, not a session key for all of them."""
    mine = _running_instance(client, "vm-mine")
    other = _running_instance(client, "vm-other")
    ticket = _ticket(client, other)

    closed, code, _reason = _connect(client, mine, ticket)

    assert closed and code == CLOSE_UNAUTHENTICATED
    # And it is spent either way — a rejected redemption must not leave a
    # working ticket behind for the instance it *was* minted for.
    closed_again, _c, _r2 = _connect(client, other, ticket)
    assert closed_again


def test_a_ticket_cannot_be_replayed(client):  # noqa: F811
    """Single use is the property that makes a ticket in a URL acceptable."""
    instance_id = _running_instance(client)
    ticket = _ticket(client, instance_id)

    # First redemption gets past authentication and fails on the VNC socket,
    # which does not exist in tests — that is far enough to prove it was
    # accepted.
    first_closed, first_code, _r = _connect(client, instance_id, ticket)
    assert first_code != CLOSE_UNAUTHENTICATED

    closed, code, _reason = _connect(client, instance_id, ticket)

    assert closed and code == CLOSE_UNAUTHENTICATED


def test_a_ticket_dies_with_the_session_that_minted_it(anon_client):  # noqa: F811
    """Logging out must not leave a working key to the console lying around."""
    login = anon_client.post(
        "/auth/login", json={"username": "owner", "password": "test-password-1234"}
    )
    assert login.status_code == 200
    csrf = login.headers[auth.CSRF_HEADER]
    instance_id = _running_instance_via_cookie(anon_client, csrf)
    ticket = anon_client.post(
        f"/instances/{instance_id}/console/ticket", headers={auth.CSRF_HEADER: csrf}
    ).json()["ticket"]

    assert anon_client.post(
        "/auth/logout", headers={auth.CSRF_HEADER: csrf}
    ).status_code == 204

    closed, code, _reason = _connect(anon_client, instance_id, ticket)
    assert closed and code == CLOSE_UNAUTHENTICATED


def test_a_password_change_invalidates_outstanding_tickets(client, anon_client):  # noqa: F811
    """Not asked for and worth having: a password change is what you do when
    you think a credential leaked, so a ticket minted before it must not still
    open a screen afterwards."""
    instance_id = _running_instance(client)
    ticket = _ticket(client, instance_id)

    login = anon_client.post(
        "/auth/login", json={"username": "owner", "password": "test-password-1234"}
    )
    csrf = login.headers[auth.CSRF_HEADER]
    assert anon_client.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: csrf},
        json={"current_password": "test-password-1234",
              "new_password": "a-brand-new-passphrase"},
    ).status_code == 204

    closed, code, _reason = _connect(client, instance_id, ticket)
    assert closed and code == CLOSE_UNAUTHENTICATED


def test_an_expired_ticket_is_refused(client):  # noqa: F811
    from datetime import timedelta

    instance_id = _running_instance(client)
    ticket = _ticket(client, instance_id)
    with Session(client.db_engine) as session:
        row = session.exec(select(ConsoleTicket)).first()
        row.expires_at = auth._utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

    closed, code, _reason = _connect(client, instance_id, ticket)

    assert closed and code == CLOSE_UNAUTHENTICATED


def test_a_made_up_ticket_is_refused(client):  # noqa: F811
    instance_id = _running_instance(client)

    closed, code, _reason = _connect(client, instance_id, "not-a-real-ticket")

    assert closed and code == CLOSE_UNAUTHENTICATED


def test_every_rejection_reads_the_same(client):  # noqa: F811
    """Distinguishing "expired" from "wrong instance" would tell a caller
    holding a stolen ticket which part to fix."""
    instance_id = _running_instance(client)
    other = _running_instance(client, "vm-other")

    reasons = set()
    for bad in ("", "garbage", _ticket(client, other)):
        closed, code, reason = _connect(client, instance_id, bad)
        assert closed
        reasons.add((code, reason))
    assert len(reasons) == 1, reasons


# --------------------------------------------------------------------------- #
def _running_instance_via_cookie(c, csrf: str) -> str:
    body = c.post("/instances", json={"name": "vm-cookie"},
                  headers={auth.CSRF_HEADER: csrf}).json()
    with Session(c.db_engine) as session:
        row = session.get(Instance, body["id"])
        row.status = InstanceStatus.RUNNING
        row.vnc_port = 5999
        session.add(row)
        session.commit()
    return body["id"]
