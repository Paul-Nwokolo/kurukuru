"""
Sign in, sign out, change password, manage API tokens.

The only routes in the product that an unauthenticated caller may reach are
here, and the list is deliberately tiny: login, and the first-run status the
dashboard needs to know whether to show a login form or a "run the setup
command" message. Everything else in the application is closed by default — see
``kurukuru.security``.

**Failures are generic on purpose.** "No such user" and "wrong password" get the
same response and the same timing cost, because the difference between them is
an account enumeration oracle. The only distinguishable failure is the lockout,
which has to say what it is or the user cannot act on it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import Session as DbSession
from sqlmodel import select

from kurukuru import auth
from kurukuru.database import get_session
from kurukuru.product import CLI_NAME, CREDENTIALS_REJECTED
from kurukuru.models import (
    ApiToken,
    ApiTokenCreate,
    ApiTokenCreated,
    ApiTokenRead,
    LoginRequest,
    PasswordChange,
    User,
    UserRead,
    validate_password,
)
from kurukuru.security import current_principal

logger = logging.getLogger("kurukuru.auth.routes")

router = APIRouter(prefix="/auth", tags=["auth"])


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_session_cookie(response: Response, secret: str) -> None:
    """httpOnly so script cannot read it; SameSite=Strict as defence in depth.

    Strict is not the CSRF defence — every localhost port is the same site, so
    it does nothing against a hostile page on another local port. That is the
    CSRF token's job. Strict is here because it costs nothing and does help
    against genuinely cross-site pages.

    ``secure`` is deliberately not set: this tool serves plain HTTP on loopback
    by default, and a Secure cookie would simply never be stored.
    """
    response.set_cookie(
        auth.SESSION_COOKIE,
        secret,
        httponly=True,
        samesite="strict",
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )
    # The cookie was called something else before Phase 16. A browser that had a
    # session open across the upgrade is still holding it, and nothing will ever
    # read it again — so it is cleared here rather than left to expire, where it
    # would sit in devtools next to the live one looking like a duplicate
    # session.
    response.delete_cookie(auth.LEGACY_SESSION_COOKIE, path="/")


@router.get("/first-run", summary="Whether an account exists yet")
def first_run_status(db: DbSession = Depends(get_session)) -> dict[str, object]:
    """Public, and carries no information an attacker can use.

    Whether *any* account exists is not a secret — an install with none is
    reachable by anyone anyway — and the dashboard needs it to choose between a
    login form and instructions for creating the owner account.

    ``cli_name`` rides along because this is the endpoint whose entire purpose
    is "tell the user which command to run", and because the dashboard needs the
    name *before* anyone is signed in. The product name is not settled; the
    dashboard prints it rather than spelling it, so a rename does not leave the
    login screen naming a command nobody has. See :mod:`kurukuru.product`.
    """
    return {
        "configured": db.exec(select(User)).first() is not None,
        "cli_name": CLI_NAME,
    }


@router.post("/login", response_model=UserRead, summary="Sign in")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession = Depends(get_session),
) -> User:
    client = _client(request)
    remaining = auth.throttle.check(payload.username, client)
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {int(remaining) + 1}s.",
            headers={"Retry-After": str(int(remaining) + 1)},
        )

    user = db.exec(select(User).where(User.username == payload.username)).first()
    # The verify runs even when the user does not exist, against a throwaway
    # hash, so that "no such user" and "wrong password" cost the same time as
    # well as returning the same message.
    stored = user.password_hash if user else auth.UNKNOWN_USER_HASH
    ok = auth.verify_password(stored, payload.password)

    if not ok or user is None:
        auth.throttle.record_failure(payload.username, client)
        logger.info("Failed login for %r from %s", payload.username, client)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=CREDENTIALS_REJECTED,
        )

    auth.throttle.record_success(payload.username, client)
    if auth.needs_rehash(user.password_hash):
        # Parameters have moved on since this hash was written; upgrade it
        # while we legitimately hold the plaintext.
        user.password_hash = auth.hash_password(payload.password)
        db.add(user)
        db.commit()
        db.refresh(user)

    session, secret = auth.create_session(db, user)
    _set_session_cookie(response, secret)
    # The CSRF token is returned in the body, not a cookie: the dashboard holds
    # it in memory and echoes it in a header. A cookie would travel
    # automatically, which is exactly what makes a cookie unsuitable here.
    response.headers[auth.CSRF_HEADER] = session.csrf_token
    logger.info("Signed in: %s", user.username)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
def logout(
    response: Response,
    principal: auth.Principal = Depends(current_principal),
    db: DbSession = Depends(get_session),
) -> Response:
    """Delete the session server-side, not merely the cookie client-side."""
    if principal.session_id:
        row = db.get(auth.Session, principal.session_id)
        if row is not None:
            db.delete(row)
            db.commit()
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.LEGACY_SESSION_COOKIE, path="/")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/whoami", response_model=UserRead, summary="The signed-in account")
def whoami(principal: auth.Principal = Depends(current_principal)) -> User:
    return principal.user


@router.get("/csrf", summary="The CSRF token for this session")
def csrf(principal: auth.Principal = Depends(current_principal)) -> dict[str, str]:
    """Lets a reloaded dashboard recover its token without signing in again.

    Readable only with the session cookie, and returned in a body rather than a
    cookie so it cannot be replayed automatically by the browser.
    """
    if principal.via != "session" or not principal.csrf_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a cookie session has a CSRF token",
        )
    return {"csrf_token": principal.csrf_token}


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT,
             summary="Change the password")
def change_password(
    payload: PasswordChange,
    response: Response,
    principal: auth.Principal = Depends(current_principal),
    db: DbSession = Depends(get_session),
) -> Response:
    """Change it, then invalidate every credential it ever issued.

    Including this caller's own session: a password change is what you do when
    you believe a credential leaked, and a flow that leaves the current session
    alive cannot tell the difference between you and whoever else had it.
    """
    user = db.get(User, principal.user.id)
    if user is None:  # pragma: no cover - the principal came from this row
        raise HTTPException(status_code=404, detail="Account not found")

    if not auth.verify_password(user.password_hash, payload.current_password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Current password is incorrect"
        )
    if auth.verify_password(user.password_hash, payload.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new password must differ from the current one",
        )

    user.password_hash = auth.hash_password(payload.new_password)
    auth.invalidate_credentials(db, user)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.delete_cookie(auth.LEGACY_SESSION_COOKIE, path="/")
    logger.info("Password changed for %s; all sessions and tokens invalidated",
                user.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# API tokens
# --------------------------------------------------------------------------- #
@router.get("/tokens", response_model=list[ApiTokenRead], summary="List API tokens")
def list_tokens(
    principal: auth.Principal = Depends(current_principal),
    db: DbSession = Depends(get_session),
) -> list[ApiToken]:
    return list(
        db.exec(
            select(ApiToken)
            .where(ApiToken.user_id == principal.user.id)
            .order_by(ApiToken.created_at)
        ).all()
    )


@router.post("/tokens", response_model=ApiTokenCreated,
             status_code=status.HTTP_201_CREATED, summary="Create an API token")
def create_token(
    payload: ApiTokenCreate,
    principal: auth.Principal = Depends(current_principal),
    db: DbSession = Depends(get_session),
) -> ApiTokenCreated:
    """The secret is in this response and nowhere else, ever again."""
    row, secret = auth.create_api_token(db, principal.user, payload.name)
    return ApiTokenCreated(
        id=row.id, name=row.name, prefix=row.prefix, created_at=row.created_at,
        last_used_at=row.last_used_at, revoked_at=row.revoked_at, token=secret,
    )


@router.delete("/tokens/{token_id}", response_model=ApiTokenRead,
               summary="Revoke an API token")
def revoke_token(
    token_id: str,
    principal: auth.Principal = Depends(current_principal),
    db: DbSession = Depends(get_session),
) -> ApiToken:
    """Revoked immediately — the next request carrying it fails.

    The row is kept and stamped rather than deleted, so a user can confirm they
    revoked the one they meant to.
    """
    row = db.get(ApiToken, token_id)
    if row is None or row.user_id != principal.user.id:
        raise HTTPException(status_code=404, detail=f"Token '{token_id}' not found")
    if row.revoked_at is None:
        row.revoked_at = auth._utcnow()
        db.add(row)
        db.commit()
        db.refresh(row)
    logger.info("Revoked API token %s (%s)", row.name, row.prefix)
    return row
