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

from app import auth
from app.database import get_session

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
    """The matched route *template*, so a parameterised path compares equal.

    ``request.url.path`` would be ``/instances/abc123/console``; the tables above
    are keyed by ``/instances/{instance_id}/console``.
    """
    route = conn.scope.get("route")
    return getattr(route, "path", None) or conn.url.path


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
            "impossible; see app.security."
        )
    return principal
