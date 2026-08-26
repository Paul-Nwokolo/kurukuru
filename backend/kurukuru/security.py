"""The application-wide guard. Closed by default; open by explicit exception.

This is an application-level dependency rather than a decorator on each router,
and the reason is the failure mode of the alternative. Per-router protection is
correct until somebody adds a router and does not know they had to; the route
then ships open, and nothing says so. Here, a route added next year is protected
the moment it is registered, and making one public takes a deliberate edit to
:data:`PUBLIC_ROUTES` — a diff a reviewer will see.

``tests/test_auth_coverage.py`` enumerates the application's routes and asserts
each one is either in that list or actually answers 401 to an anonymous caller,
so the guarantee is checked rather than asserted.
"""

from __future__ import annotations

import logging

from fastapi import Depends
from starlette.requests import HTTPConnection
from sqlmodel import Session as DbSession

from kurukuru import auth
from kurukuru.database import get_session
from kurukuru.product import API_PREFIX

logger = logging.getLogger("kurukuru.security")

#: Paths reachable without a credential, each with the reason it has to be.
#:
#: Keep this list short and keep the reasons honest. Anything added here is a
#: piece of the product an unauthenticated caller can reach.
PUBLIC_ROUTES: dict[str, str] = {
    "/health": (
        "liveness for the dashboard's status indicator and for process "
        "supervisors. Reports whether the service and hypervisor are up, and "
        "nothing about the instances on it."
    ),
    "/auth/login": "you cannot authenticate to obtain authentication",
    "/auth/first-run": (
        "lets the dashboard choose between a login form and setup instructions. "
        "Reports only whether any account exists, which an install with none "
        "cannot hide anyway."
    ),
    "/{full_path:path}": (
        "the dashboard's own files. It has to be reachable unauthenticated for "
        "the obvious reason: it *is* the login screen, and a sign-in page behind "
        "a sign-in is not a page. What it serves is a static JavaScript bundle "
        "and its assets — the same bytes for every visitor, containing no "
        "instance data. Everything the bundle then asks for goes through the "
        "API, which is not public, so an anonymous visitor gets an application "
        "shell that can do nothing but offer them a login form. Note this entry "
        "is the *catch-all*, matched only after every API route has declined; "
        "it cannot widen anything above it."
    ),
}

#: Deliberately NOT public: ``/openapi.json``, ``/docs``, ``/redoc`` and
#: ``/docs/oauth2-redirect``. They carry no instance data, but they do describe
#: every route in the product, and an anonymous caller has no use for them —
#: signed in, the browser's cookie makes Swagger UI work exactly as before. The
#: route-coverage test found ``/docs/oauth2-redirect`` on its first run, which
#: is the argument for enumerating routes rather than listing them by hand.

#: Handled inside the endpoint rather than here. A WebSocket cannot carry an
#: Authorization header from a browser, and an HTTPException raised in a
#: dependency cannot become a WebSocket close frame — so the console
#: authenticates by redeeming a single-use ticket on connect.
TICKET_ROUTES: dict[str, str] = {
    "/instances/{instance_id}/console": "authenticated by console ticket on connect",
}


def route_path(conn: HTTPConnection) -> str:
    """The matched route *template*, with the API prefix removed.

    Two normalisations, both so the tables above stay readable:

    ``request.url.path`` would be ``/api/instances/abc123/console``; the tables
    are keyed by the route template, ``/instances/{instance_id}/console``.

    And the prefix is stripped rather than written into every key. The tables
    say *why a route is public*, which is a fact about the route and not about
    where the API happens to be mounted — keying them by the mount point would
    mean moving it silently unprotects everything, since a path that matches no
    key is simply not public. Stripping fails the other way: safe.
    """
    route = conn.scope.get("route")
    return table_key(getattr(route, "path", None) or conn.url.path)


def table_key(path: str) -> str:
    """A route template as the tables above spell it: without the API prefix.

    Exported because ``tests/test_auth_coverage.py`` enumerates the application's
    real routes and has to look each one up in those tables. If it normalised
    paths its own way and the two ever diverged, the coverage test would report
    green while checking nothing — so both go through this.
    """
    if path.startswith(API_PREFIX):
        return path[len(API_PREFIX):] or "/"
    return path


def is_public(path: str) -> bool:
    return path in PUBLIC_ROUTES


def guard(
    conn: HTTPConnection,
    db: DbSession = Depends(get_session),
) -> None:
    """Registered on the application, so it runs for every route.

    The parameter is an ``HTTPConnection`` — the common base of ``Request`` and
    ``WebSocket`` — because an application-level dependency is applied to
    WebSocket routes too, and FastAPI injects by exact type. A ``Request``-only
    signature raises ``TypeError: missing 1 required positional argument`` the
    moment a browser opens the console, and a ``Request | None`` union is
    rejected outright at import time. Both were measured.

    WebSocket scopes are handed straight back: an HTTPException cannot become a
    close frame, so the console authenticates by redeeming a ticket inside the
    endpoint instead.

    Stores the principal on ``conn.state`` so a handler that wants to know who
    is calling does not pay for a second lookup.
    """
    if conn.scope.get("type") != "http":
        return
    path = route_path(conn)
    if path in PUBLIC_ROUTES:
        return
    if path in TICKET_ROUTES:
        return
    conn.state.principal = auth.require_user(conn, db)


def current_principal(conn: HTTPConnection) -> auth.Principal:
    """The authenticated caller, for handlers that need it.

    Reads what :func:`guard` already resolved. It cannot be missing on a
    protected route — the guard runs first and raises — so an absence here is a
    routing mistake worth failing loudly on rather than re-authenticating and
    hiding.
    """
    principal = getattr(conn.state, "principal", None)
    if principal is None:  # pragma: no cover - defensive
        raise RuntimeError(
            "No principal on the request. A route reached current_principal "
            "without passing through the application guard, which should be "
            "impossible; see kurukuru.security."
        )
    return principal
