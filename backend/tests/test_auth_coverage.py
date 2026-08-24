"""Every route is closed unless it is deliberately open.

This is the test the phase is built around. Authentication that covers the
routes somebody remembered is not a security property — the one that ships open
is always the one added last, by someone who did not know there was a list to
add it to. So nothing here names a route: it enumerates what the application
actually registered and asserts each entry is either

  * in :data:`app.security.PUBLIC_ROUTES`, with a written reason, or
  * in :data:`app.security.TICKET_ROUTES`, authenticated by its own mechanism, or
  * genuinely answering 401 to an anonymous caller.

A route added next year is covered the moment it is registered. If it is open,
this fails and names it.
"""

from __future__ import annotations

import pytest
from starlette.routing import Route, WebSocketRoute

from app.main import app
from app.security import PUBLIC_ROUTES, TICKET_ROUTES

from tests.test_instances_api import anon_client, client, iso_dir  # noqa: F401

#: Path parameters get a value that is syntactically fine and refers to nothing.
#: The guard runs before the endpoint, so an anonymous caller must get 401 and
#: never 404 — reaching "not found" would mean the lookup happened first, which
#: is itself an information leak.
_PARAM_VALUE = "00000000-0000-0000-0000-000000000000"


def _concrete(path: str) -> str:
    out = path
    while "{" in out:
        start = out.index("{")
        end = out.index("}", start)
        out = out[:start] + _PARAM_VALUE + out[end + 1:]
    return out


def _http_routes() -> list[tuple[str, str]]:
    """(template, method) for every HTTP route the app registered."""
    pairs: list[tuple[str, str]] = []
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue  # CORS preflight and HEAD are not separate surfaces
            pairs.append((route.path, method))
    return pairs


def _websocket_routes() -> list[str]:
    return [r.path for r in app.routes if isinstance(r, WebSocketRoute)]


def test_the_application_actually_has_routes():
    """Guards the guard: if enumeration broke, every assertion below would pass
    vacuously and this file would be worse than useless."""
    assert len(_http_routes()) > 40


@pytest.mark.parametrize("path,method", _http_routes(), ids=lambda v: str(v))
def test_every_route_is_closed_or_deliberately_public(anon_client, path, method):  # noqa: F811
    if path in PUBLIC_ROUTES:
        assert PUBLIC_ROUTES[path].strip(), f"{path} is public with no stated reason"
        return
    if path in TICKET_ROUTES:
        return

    response = anon_client.request(method, _concrete(path))

    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} to an anonymous caller. "
        f"Every route is closed by default; if this one must be open, add it to "
        f"app.security.PUBLIC_ROUTES with the reason."
    )


def test_public_routes_are_all_real_routes():
    """A stale entry is a hole waiting for a path to be reused."""
    registered = {path for path, _ in _http_routes()}
    for path in PUBLIC_ROUTES:
        assert path in registered, f"PUBLIC_ROUTES names {path}, which no longer exists"


def test_ticket_routes_are_all_real_routes():
    registered = set(_websocket_routes()) | {p for p, _ in _http_routes()}
    for path in TICKET_ROUTES:
        assert path in registered, f"TICKET_ROUTES names {path}, which no longer exists"


def test_the_console_websocket_is_not_quietly_public():
    """It is exempt from the HTTP guard, so its own mechanism has to be real.
    Rejection behaviour is asserted in test_console_auth.py; this only checks
    the exemption is declared rather than accidental."""
    for path in _websocket_routes():
        assert path in TICKET_ROUTES, (
            f"WebSocket {path} is outside the HTTP guard and not declared in "
            f"TICKET_ROUTES — it would be unauthenticated."
        )


def test_health_is_public_and_says_nothing_about_instances(anon_client):  # noqa: F811
    """The one route an anonymous caller can read. It must stay a liveness
    probe rather than becoming an inventory."""
    body = anon_client.get("/health").json()

    assert anon_client.get("/health").status_code == 200
    serialised = str(body).lower()
    for leak in ("instance", "vm-", "192.168", "ssh_port"):
        assert leak not in serialised, f"/health leaked {leak!r} to an anonymous caller"
