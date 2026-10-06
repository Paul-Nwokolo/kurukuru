"""The browser-facing defences refuse what they exist to refuse.

Every other test in the suite talks to the application the way a legitimate
client does — loopback Host, no foreign Origin — so it passes *through*
TrustedHostMiddleware, CORS and the security headers without ever asking them
to say no. Phase 17 measured what that costs: with TrustedHost removed, with
CORS opened to every origin, or with the security headers gone, the full suite
still reported 1058 passed. A framework upgrade that quietly changed any of the
three would have shipped green.

So each test here sends the request the defence exists to stop, and was
watched to fail with that defence broken (DECISIONS #65).

The last section is the reason Phase 17 happened: the dashboard's asset route
answers ``Range`` through Starlette's ``FileResponse``, unauthenticated, and
before Starlette 0.49.1 a crafted header made that parse quadratic, on the
event loop (PYSEC-2026-1942).
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from tests.conftest import api_ws
from tests.test_dashboard import built, served  # noqa: F401
from tests.test_instances_api import anon_client, client, iso_dir  # noqa: F401

#: A page on another local port — the shape DECISIONS #45 and #51 measured.
#: Same *site* as the backend, which is why CORS and the CSRF token, not
#: SameSite, are what stand between it and the API.
OTHER_LOCAL_PORT = "http://127.0.0.1:8099"


# --------------------------------------------------------------------------- #
# DNS rebinding: the Host header
# --------------------------------------------------------------------------- #
def test_a_rebound_host_is_refused_before_any_route_runs(anon_client):  # noqa: F811
    """`/health` is public, so nothing but the Host check can refuse this.

    A rebinding attack makes the attacker's domain resolve to 127.0.0.1; their
    page is then same-origin with this server and can read the CSRF token. The
    Host header still carries their name, and it is the last thing that does.
    """
    response = anon_client.get("/health", headers={"host": "rebind.attacker.invalid"})

    assert response.status_code == 400
    assert "status" not in response.text


@pytest.mark.parametrize("host", ["127.0.0.1:7842", "localhost:7842", "127.0.0.1", "localhost"])
def test_the_loopback_names_are_accepted_with_or_without_a_port(anon_client, host):  # noqa: F811
    """The other half: refusing too much would look like a networking fault.

    No IPv6 literal here, because none is accepted. Starlette's
    TrustedHostMiddleware splits the header on its *first* colon, so both
    ``[::1]`` and ``[::1]:7842`` arrive at the comparison as ``"["`` and the
    ``::1`` entries in the allow-list never match. Identical on 0.46.2; reachable
    only by binding ``--host ::1``. Recorded in DECISIONS #65, not fixed in a
    phase whose brief is to change dependencies and nothing else.
    """
    assert anon_client.get("/health", headers={"host": host}).status_code == 200


def test_a_rebound_host_cannot_open_the_console(client):  # noqa: F811
    """The WebSocket handshake goes through the same middleware stack.

    The assertion is on *how* it is refused, because the endpoint refuses a
    ticketless caller too — it accepts and then closes with 4401. Asserting
    only "it disconnected" passed with TrustedHost removed; a denial of the
    handshake itself, before accept, is what only the Host check produces.
    """
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(
            api_ws("/instances/00000000-0000-0000-0000-000000000000/console"),
            headers={"host": "rebind.attacker.invalid"},
        ):
            pass

    assert denied.value.status_code == 400


# --------------------------------------------------------------------------- #
# CORS ships empty
# --------------------------------------------------------------------------- #
def test_a_preflight_from_another_local_port_is_refused(anon_client):  # noqa: F811
    """DECISIONS #51 measured exactly this in Chrome: preflight → 400, so a
    credentialed `fetch()` from another port is blocked before it is sent."""
    response = anon_client.options(
        "/projects",
        headers={
            "origin": OTHER_LOCAL_PORT,
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert response.status_code == 400
    # Allow-Origin is the header that decides. Starlette still sends
    # Allow-Credentials on a refused preflight (it did on 0.46.2 too), which
    # grants nothing: the browser requires an Allow-Origin that matches.
    assert "access-control-allow-origin" not in response.headers


def test_a_simple_request_from_another_local_port_gets_no_cors_grant(anon_client):  # noqa: F811
    """Without an allow-origin header the browser withholds the response from
    the page that asked, even though the request itself was sent."""
    response = anon_client.get("/health", headers={"origin": OTHER_LOCAL_PORT})

    assert "access-control-allow-origin" not in response.headers


# --------------------------------------------------------------------------- #
# Security headers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path,expected", [("/health", 200), ("/instances", 401)])
def test_security_headers_are_on_every_response(anon_client, path, expected):  # noqa: F811
    """Including refusals — a header present everywhere cannot be missing from
    the one response that turns out to matter."""
    response = anon_client.get(path)

    assert response.status_code == expected
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "connect-src 'self'" in csp


# --------------------------------------------------------------------------- #
# Range on the dashboard's assets (PYSEC-2026-1942)
# --------------------------------------------------------------------------- #
def test_an_ordinary_range_request_still_gets_a_partial_response(served: TestClient):  # noqa: F811
    """Range support is not something to fix by removing: media players and
    resumed downloads use it, and the asset route serves real files."""
    response = served.get("/assets/index-abc123.js", headers={"range": "bytes=0-6"})

    assert response.status_code == 206
    assert response.content == b"console"


@pytest.mark.parametrize(
    "header",
    [
        "bytes=" + "1" * 32_000,
        # On 0.46.2 this one was not slow but fatal: an unhandled ValueError
        # from int() on a 16,000-digit string, so a 500 on an unauthenticated
        # route. Measured in Phase 17.
        "bytes=-" + "1" * 16_000,
    ],
    ids=["digit-run", "suffix-digit-run"],
)
def test_a_hostile_range_header_is_refused_cheaply(served: TestClient, header: str):  # noqa: F811
    """The quadratic parse, measured on this host before the upgrade:
    8k digits 0.26 s, 16k 0.81 s, 32k 4.69 s — synchronously, on the event
    loop, so every other request waits. Fixed, 32k takes about 2 ms.

    A timing assertion, which this project normally avoids. It is used here
    because the property *is* time, and the margins are wide on both sides:
    the ceiling is ~5x under the vulnerable cost and ~500x over the fixed one,
    and a slower machine only makes the vulnerable case slower still.
    """
    started = time.perf_counter()
    response = served.get("/assets/index-abc123.js", headers={"range": header})
    elapsed = time.perf_counter() - started

    assert response.status_code in (400, 416)
    assert elapsed < 1.0, f"Range parse took {elapsed:.2f}s"
