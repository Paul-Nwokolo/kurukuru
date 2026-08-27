"""Sessions, CSRF, tokens, and the ways each of them must stop working.

The CSRF assertions here are the server-side half of a measurement taken in a
real browser, and they exist because the reasoning behind them is easy to get
backwards. Chrome, two pages on different localhost ports, one backend:

    login from http://localhost:8101          -> 200, httpOnly cookie set
    form POST from http://localhost:8102      -> 403, not 401

401 would have meant the cookie was never sent — SameSite treating the two
ports as different sites. **403 means the cookie was sent** and the CSRF token
is what refused it. Port is not part of a site, so every localhost port is
same-site with this backend and ``SameSite=Strict`` stops none of them. The CSRF
token is not belt-and-braces here; it is the only thing standing there, and no
amount of same-origin packaging changes that (DECISIONS #45).
"""

from __future__ import annotations

import pathlib

import pytest

from kurukuru import auth
from kurukuru.models import ApiToken, Session as SessionRow, User
from sqlmodel import Session, select

from tests.test_instances_api import anon_client, client, iso_dir  # noqa: F401

PASSWORD = "test-password-1234"


@pytest.fixture(autouse=True)
def _clear_throttle():
    auth.throttle.reset()
    yield
    auth.throttle.reset()


def _login(c, username: str = "owner", password: str = PASSWORD):
    return c.post("/auth/login", json={"username": username, "password": password})


@pytest.fixture()
def browser(anon_client):  # noqa: F811
    """A client authenticated the way a browser is: cookie plus CSRF token."""
    response = _login(anon_client)
    assert response.status_code == 200, response.text
    anon_client.csrf = response.headers[auth.CSRF_HEADER]  # type: ignore[attr-defined]
    return anon_client


# --------------------------------------------------------------------------- #
# The cookie
# --------------------------------------------------------------------------- #
def test_login_sets_an_httponly_cookie(anon_client):  # noqa: F811
    response = _login(anon_client)

    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert auth.SESSION_COOKIE in cookie
    # httpOnly is what stops a hostile script reading the session outright.
    # Confirmed in a real browser too: document.cookie was empty after login.
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()


def test_a_cookie_session_can_read(browser):
    assert browser.get("/instances").status_code == 200


# --------------------------------------------------------------------------- #
# CSRF — the browser measurement, pinned
# --------------------------------------------------------------------------- #
def test_a_cookie_write_without_the_csrf_token_is_refused(browser):
    """The exact case a hostile page on another localhost port produces."""
    response = browser.post("/instances/refresh")

    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_a_cookie_write_with_the_csrf_token_succeeds(browser):
    response = browser.post(
        "/instances/refresh", headers={auth.CSRF_HEADER: browser.csrf}
    )

    assert response.status_code == 200


def test_a_wrong_csrf_token_is_refused(browser):
    response = browser.post(
        "/instances/refresh", headers={auth.CSRF_HEADER: "not-the-right-token"}
    )

    assert response.status_code == 403


def test_401_and_403_really_do_discriminate(anon_client, browser):  # noqa: F811
    """The discriminator the browser test relied on.

    If a missing credential and a missing CSRF token both returned the same
    status, the 403 observed in Chrome would have proved nothing about whether
    the cookie was sent.
    """
    browser.cookies.clear()
    assert browser.post("/instances/refresh").status_code == 401


def test_bearer_writes_need_no_csrf_token(client):  # noqa: F811
    """Not a hole: a browser cannot be made to attach an Authorization header
    to a cross-origin request, so its presence is proof of intent."""
    assert client.post("/instances/refresh").status_code == 200


# --------------------------------------------------------------------------- #
# Ending a session
# --------------------------------------------------------------------------- #
def test_logout_invalidates_the_session_server_side(browser):
    assert browser.post("/auth/logout", headers={auth.CSRF_HEADER: browser.csrf}).status_code == 204

    # Even replaying the cookie by hand does not work: the row is gone.
    assert browser.get("/instances").status_code == 401


def test_password_change_invalidates_every_session_and_token(browser, client):  # noqa: F811
    """A password change is what you do when you think a credential leaked, so
    a flow that leaves the old ones alive has missed the point."""
    token_before = client.headers["Authorization"]

    response = browser.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: browser.csrf},
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
    )
    assert response.status_code == 204, response.text

    # The session that made the change is gone too.
    assert browser.get("/instances").status_code == 401
    # And so is the API token issued before it.
    client.headers["Authorization"] = token_before
    assert client.get("/instances").status_code == 401


def test_the_old_password_stops_working_and_the_new_one_starts(browser, anon_client):  # noqa: F811
    browser.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: browser.csrf},
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
    )
    anon_client.cookies.clear()

    assert _login(anon_client, password=PASSWORD).status_code == 401
    assert _login(anon_client, password="a-brand-new-passphrase").status_code == 200


def test_changing_to_the_same_password_is_refused(browser):
    response = browser.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: browser.csrf},
        json={"current_password": PASSWORD, "new_password": PASSWORD},
    )

    assert response.status_code == 400


def test_a_wrong_current_password_cannot_change_it(browser):
    response = browser.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: browser.csrf},
        json={"current_password": "wrong-password-here", "new_password": "another-passphrase"},
    )

    assert response.status_code == 403


def test_a_short_new_password_is_refused(browser):
    response = browser.post(
        "/auth/password",
        headers={auth.CSRF_HEADER: browser.csrf},
        json={"current_password": PASSWORD, "new_password": "short"},
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# API tokens
# --------------------------------------------------------------------------- #
def test_a_token_is_shown_once_and_never_again(client):  # noqa: F811
    created = client.post("/auth/tokens", json={"name": "laptop"})
    assert created.status_code == 201
    secret = created.json()["token"]

    listed = client.get("/auth/tokens").json()
    assert all("token" not in row for row in listed)
    # And the stored form is not the secret.
    with Session(client.db_engine) as session:
        rows = session.exec(select(ApiToken)).all()
        assert all(row.token_hash != secret for row in rows)


def test_a_new_token_authenticates(client, anon_client):  # noqa: F811
    secret = client.post("/auth/tokens", json={"name": "laptop"}).json()["token"]
    anon_client.headers["Authorization"] = f"Bearer {secret}"

    assert anon_client.get("/instances").status_code == 200


def test_revoking_a_token_stops_it_immediately(client, anon_client):  # noqa: F811
    created = client.post("/auth/tokens", json={"name": "laptop"}).json()
    anon_client.headers["Authorization"] = f"Bearer {created['token']}"
    assert anon_client.get("/instances").status_code == 200

    assert client.delete(f"/auth/tokens/{created['id']}").status_code == 200

    assert anon_client.get("/instances").status_code == 401


def test_a_revoked_token_is_still_listed(client):  # noqa: F811
    created = client.post("/auth/tokens", json={"name": "laptop"}).json()
    client.delete(f"/auth/tokens/{created['id']}")

    rows = {r["id"]: r for r in client.get("/auth/tokens").json()}
    assert rows[created["id"]]["revoked_at"] is not None


def test_a_made_up_token_is_rejected(anon_client):  # noqa: F811
    anon_client.headers["Authorization"] = "Bearer kurukuru_not-a-real-token"

    assert anon_client.get("/instances").status_code == 401


# --------------------------------------------------------------------------- #
# Login failures
# --------------------------------------------------------------------------- #
def test_an_unknown_user_and_a_wrong_password_are_indistinguishable(anon_client):  # noqa: F811
    """Anything that tells the two apart is an account enumeration oracle."""
    unknown = _login(anon_client, username="nobody", password="whatever-1234")
    wrong = _login(anon_client, password="wrong-password-here")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_repeated_failures_lock_the_account_out(anon_client):  # noqa: F811
    for _ in range(auth.throttle.limit):
        _login(anon_client, password="wrong-password-here")

    # Even the correct password is refused while the lockout holds.
    response = _login(anon_client)
    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_the_lockout_releases(anon_client, monkeypatch):  # noqa: F811
    for _ in range(auth.throttle.limit):
        _login(anon_client, password="wrong-password-here")
    assert _login(anon_client).status_code == 429

    # Rather than sleeping out the window, move the clock the throttle reads.
    import time as _time

    later = _time.monotonic() + auth.throttle.window + 1
    monkeypatch.setattr(auth.time, "monotonic", lambda: later)

    assert _login(anon_client).status_code == 200


def test_a_successful_login_clears_the_failure_count(anon_client):  # noqa: F811
    for _ in range(auth.throttle.limit - 1):
        _login(anon_client, password="wrong-password-here")
    assert _login(anon_client).status_code == 200

    for _ in range(auth.throttle.limit - 1):
        _login(anon_client, password="wrong-password-here")
    assert _login(anon_client).status_code == 200


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_whoami_reports_the_account_and_never_the_hash(client):  # noqa: F811
    body = client.get("/auth/whoami").json()

    assert body["username"] == "owner"
    assert body["is_owner"] is True
    assert "password_hash" not in body


def test_first_run_reports_configured_once_an_account_exists(anon_client):  # noqa: F811
    assert anon_client.get("/auth/first-run").json()["configured"] is True


def test_first_run_tells_the_dashboard_what_the_command_is_called(anon_client):  # noqa: F811
    """The dashboard prints the CLI's name; it must not spell it itself.

    The product name is not settled. The login screen's instructions ("run
    ``<cli> auth init``") are shown to someone who is, by definition, locked
    out and following them literally — so a rename that left the dashboard
    naming the old command would break the one path that has no other way
    through. This is the field that stops the name being duplicated in TSX.
    """
    from kurukuru.product import CLI_NAME

    assert anon_client.get("/auth/first-run").json()["cli_name"] == CLI_NAME


def test_the_cli_name_has_exactly_one_definition():
    """``kurukuru.cli.naming`` re-exports it rather than holding a second copy.

    The re-export exists so the hundred CLI modules that already import from
    ``naming`` keep working. If someone later "tidies" it back into a literal,
    the backend and the CLI can drift to different names without any test
    noticing — this is the one that notices.
    """
    from kurukuru import product
    from kurukuru.cli import naming

    assert naming.CLI_NAME is product.CLI_NAME

    source = (pathlib.Path(naming.__file__)).read_text(encoding="utf-8")
    assert 'CLI_NAME = "' not in source, "naming.py should import the name, not define it"


def test_an_expired_session_is_rejected_and_cleaned_up(browser):
    from datetime import timedelta

    with Session(browser.db_engine) as session:
        row = session.exec(select(SessionRow)).first()
        row.expires_at = auth._utcnow() - timedelta(seconds=1)
        session.add(row)
        session.commit()

    assert browser.get("/instances").status_code == 401

    with Session(browser.db_engine) as session:
        assert session.exec(select(SessionRow)).first() is None


# --------------------------------------------------------------------------- #
# Which 401 is it?
# --------------------------------------------------------------------------- #
def test_a_rejected_password_names_reset_password_not_login(anon_client):  # noqa: F811
    """The advice must not send the user back to the command that just failed.

    Two different things return 401. A credential that was *absent or
    unrecognised* means "sign in", and `auth login` is the fix. A credential
    that was *presented and rejected* means the password is wrong — and telling
    that user to run `auth login` is a loop with no exit, which reads like the
    tool did not notice they had just tried exactly that.
    """
    from kurukuru.cli.client import ApiClient
    from kurukuru.cli.errors import CliError

    with pytest.raises(CliError) as exc:
        ApiClient("http://testserver", http=anon_client).login("owner", "wrong-password")

    assert "Incorrect username or password" in exc.value.message
    assert "reset-password" in (exc.value.hint or ""), exc.value.hint
    assert "auth login" not in (exc.value.hint or ""), (
        "the hint sends the user back to the command that just refused them"
    )


def test_an_unknown_user_gets_the_same_answer_as_a_wrong_password(anon_client):  # noqa: F811
    """Whether an account exists is not something a failed login may reveal."""
    from kurukuru.cli.client import ApiClient
    from kurukuru.cli.errors import CliError

    errors = []
    for username in ("owner", "no-such-user"):
        with pytest.raises(CliError) as exc:
            ApiClient("http://testserver", http=anon_client).login(username, "wrong")
        errors.append((exc.value.message, exc.value.hint))

    assert errors[0] == errors[1]


def test_a_missing_credential_still_says_sign_in(anon_client):  # noqa: F811
    """The other half. Sending this user to reset-password would be wrong —
    there is nothing wrong with their password; they have not presented one."""
    from kurukuru.cli.client import ApiClient
    from kurukuru.cli.errors import CliError

    with pytest.raises(CliError) as exc:
        ApiClient("http://testserver", http=anon_client, token="kurukuru_nonsense").request(
            "GET", "/auth/whoami"
        )

    assert "Not authenticated" in exc.value.message
    assert "auth login" in (exc.value.hint or "")
    assert "reset-password" not in (exc.value.hint or "")


def test_the_cli_and_the_api_agree_on_the_rejection_detail(anon_client):  # noqa: F811
    """The CLI branches on the server's wording, so a drift silently reinstates
    the loop: the generic branch would win and every rejected password would be
    told to sign in again.

    Asserted against the **response**, not the source. A source check passes as
    soon as the constant is imported anywhere, whether or not it is what the
    route actually returns.
    """
    from kurukuru.product import CREDENTIALS_REJECTED

    response = _login(anon_client, password="wrong-password")

    assert response.status_code == 401
    assert response.json()["detail"] == CREDENTIALS_REJECTED
