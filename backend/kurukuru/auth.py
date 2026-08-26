"""Authentication: who are you. Not authorization — every account is equal.

Three credentials reach this module, and they are deliberately not
interchangeable:

* a **session cookie**, for the browser. httpOnly, so script cannot read it;
  paired with a CSRF token, because a cookie alone is not enough (see below).
* an **API token**, for the CLI and scripts, in ``Authorization: Bearer``.
  Never in a query string — a URL ends up in logs, history and ``Referer``.
* a **console ticket**, minted over authenticated HTTP and redeemed once on the
  WebSocket handshake, because browsers cannot set headers on that handshake.

**Why CSRF protection is permanent here, not a stopgap until packaging.**
``SameSite`` is computed from scheme and registrable domain — *port is not part
of a site*. So ``http://localhost:9999`` and ``http://localhost:8000`` are the
same site, and a cookie set by this backend is sent to requests originating from
any other page on any other local port: a stale dev server, a docs preview, a
package's build tool. ``SameSite=Strict`` does not defend a localhost-bound tool
against a local hostile page, and no amount of same-origin packaging changes
that. The CSRF token does, so state-changing requests authenticated by cookie
must carry it (DECISIONS #45).

Bearer-token requests are exempt from the CSRF check, and that is not a hole: a
browser cannot be tricked into attaching an ``Authorization`` header to a
cross-origin request, so the token's presence is itself proof the caller
intended it.

**Password change invalidates everything**, via a ``credential_version`` on the
user that every session and token carries a copy of. One write invalidates the
lot: no sweep to get wrong, no window where an old cookie outlives the password
it was obtained with.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from starlette.requests import HTTPConnection
from sqlmodel import Session as DbSession
from sqlmodel import select

from kurukuru.database import get_session
#: Re-exported: ``CSRF_HEADER`` is defined in :mod:`kurukuru.product` because the
#: CLI has to send it and may not import this module. Imported here so that
#: ``auth.CSRF_HEADER`` keeps working for every existing call site.
from kurukuru.product import CSRF_HEADER
from kurukuru.models import ApiToken, ConsoleTicket, Session, User

logger = logging.getLogger("kurukuru.auth")

#: The browser's cookie. Named without a leading ``__Host-`` prefix deliberately:
#: that prefix requires Secure, and this tool is served over plain HTTP on
#: loopback by default. Revisit if TLS ever becomes the norm here.
SESSION_COOKIE = "kurukuru_session"
#: What the cookie was called before Phase 16. Cleared alongside the new one
#: on login and logout so an upgraded browser is not left holding a cookie no
#: route will ever read again. Nothing authenticates against it: renaming the
#: cookie logs every open browser session out exactly once, which is the whole
#: cost of not shipping the old name forever.
LEGACY_SESSION_COOKIE = "iaas_session"
#: Prefix on every API token, so a leaked string is recognisable as one and can
#: be grepped for in logs and repositories. Renamed in Phase 16 without a
#: compatibility path, and safe because it is written at *issue* time only —
#: presentation is checked by hash, so a token issued as ``iaas_…`` keeps
#: working until it is revoked.
TOKEN_PREFIX = "kurukuru_"

_hasher = PasswordHasher()

#: Verified against when the named account does not exist, so that a login for
#: an unknown user costs the same argon2 work as one for a real user. Without
#: it, response time alone answers "does this account exist?".
UNKNOWN_USER_HASH = _hasher.hash("no such user")


# --------------------------------------------------------------------------- #
# Passwords and secrets
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """argon2id, with the library's defaults, which track current guidance."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Constant-time by construction — argon2 compares the derived key itself.

    Returns False rather than raising for *every* failure, including a hash this
    build cannot parse. A malformed row must not be distinguishable from a wrong
    password by the shape of the response.
    """
    try:
        _hasher.verify(stored_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False


def generate_secret(nbytes: int = 32) -> str:
    """A high-entropy credential. 32 bytes is 256 bits of randomness."""
    return secrets.token_urlsafe(nbytes)


def hash_secret(secret: str) -> str:
    """SHA-256, and deliberately *not* a KDF.

    A KDF exists to make guessing a low-entropy secret expensive. These secrets
    are 256 random bits — there is no dictionary to slow down — and this runs on
    every authenticated request. argon2 here would be a self-inflicted rate
    limit on the whole API while adding nothing.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def secrets_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; comparisons need them aware."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Login throttling
# --------------------------------------------------------------------------- #
@dataclass
class _Attempts:
    count: int = 0
    blocked_until: float = 0.0


class LoginThrottle:
    """Fixed lockout after repeated failures, keyed by username and by client.

    In process memory, not the database: this is a rate limit, not an audit
    trail, and a restart clearing it is acceptable — an attacker who can restart
    the backend has already won. Keyed by *both* username and source so that
    hammering one account cannot lock out another user's ability to sign in.

    A local tool is not exempt from this. The threat named in the brief is a
    malicious page or process on the same machine, which can try passwords far
    faster than a person.
    """

    def __init__(self, limit: int = 10, window_seconds: int = 300) -> None:
        self.limit = limit
        self.window = window_seconds
        self._state: dict[str, _Attempts] = {}
        self._lock = threading.Lock()

    def _key(self, username: str, client: str) -> str:
        return f"{username.lower()}|{client}"

    def check(self, username: str, client: str) -> float:
        """Seconds remaining on a lockout, or 0.0 when the caller may proceed."""
        with self._lock:
            entry = self._state.get(self._key(username, client))
            if entry is None or entry.blocked_until <= time.monotonic():
                return 0.0
            return entry.blocked_until - time.monotonic()

    def record_failure(self, username: str, client: str) -> None:
        with self._lock:
            key = self._key(username, client)
            entry = self._state.setdefault(key, _Attempts())
            entry.count += 1
            if entry.count >= self.limit:
                entry.blocked_until = time.monotonic() + self.window
                entry.count = 0
                logger.warning(
                    "Login locked out for %ss after %d failures (%s)",
                    self.window, self.limit, key,
                )

    def record_success(self, username: str, client: str) -> None:
        with self._lock:
            self._state.pop(self._key(username, client), None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


throttle = LoginThrottle()


# --------------------------------------------------------------------------- #
# Issuing and resolving credentials
# --------------------------------------------------------------------------- #
#: How long a browser session lasts without being seen.
SESSION_TTL = timedelta(days=7)
#: A console ticket is redeemed within seconds of being minted. Anything longer
#: is a credential lying around for no reason.
CONSOLE_TICKET_TTL = timedelta(seconds=30)


@dataclass(frozen=True)
class Principal:
    """Who the request is, and how it proved it.

    ``via`` is not decoration: it decides whether the CSRF check applies, and it
    is what lets a console ticket be bound to the session that minted it.
    """

    user: User
    via: str            # "session" | "token"
    session_id: str | None = None
    csrf_token: str | None = None


def create_session(db: DbSession, user: User) -> tuple[Session, str]:
    """Returns the row and the secret. The secret is never stored."""
    secret = generate_secret()
    row = Session(
        user_id=user.id,
        token_hash=hash_secret(secret),
        csrf_token=generate_secret(16),
        credential_version=user.credential_version,
        expires_at=_utcnow() + SESSION_TTL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, secret


def create_api_token(db: DbSession, user: User, name: str) -> tuple[ApiToken, str]:
    secret = TOKEN_PREFIX + generate_secret()
    row = ApiToken(
        user_id=user.id,
        name=name,
        token_hash=hash_secret(secret),
        prefix=secret[: len(TOKEN_PREFIX) + 6],
        credential_version=user.credential_version,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, secret


def invalidate_credentials(db: DbSession, user: User) -> None:
    """Bump the user's credential version. Everything issued before dies.

    Sessions are deleted outright as well, so ``auth token ls`` does not show a
    list of rows that merely happen to be unusable. Tokens are left with the old
    version rather than deleted: the user should see that their tokens stopped
    working, not find them silently gone.
    """
    user.credential_version += 1
    user.updated_at = _utcnow()
    db.add(user)
    for row in db.exec(select(Session).where(Session.user_id == user.id)).all():
        db.delete(row)
    db.commit()


def _principal_from_session(db: DbSession, secret: str) -> Principal | None:
    row = db.exec(
        select(Session).where(Session.token_hash == hash_secret(secret))
    ).first()
    if row is None:
        return None
    if _aware(row.expires_at) <= _utcnow():
        db.delete(row)
        db.commit()
        return None
    user = db.get(User, row.user_id)
    if user is None or user.credential_version != row.credential_version:
        # The password changed under it. Delete rather than leave a row that
        # will fail this check on every future request.
        db.delete(row)
        db.commit()
        return None
    row.last_seen_at = _utcnow()
    db.add(row)
    db.commit()
    return Principal(user=user, via="session", session_id=row.id,
                     csrf_token=row.csrf_token)


def _principal_from_token(db: DbSession, secret: str) -> Principal | None:
    row = db.exec(
        select(ApiToken).where(ApiToken.token_hash == hash_secret(secret))
    ).first()
    if row is None or row.revoked_at is not None:
        return None
    user = db.get(User, row.user_id)
    if user is None or user.credential_version != row.credential_version:
        return None
    row.last_used_at = _utcnow()
    db.add(row)
    db.commit()
    return Principal(user=user, via="token")


def resolve_principal(conn: HTTPConnection, db: DbSession) -> Principal | None:
    """Identify the caller, or None. Never raises for a bad credential."""
    header = conn.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return _principal_from_token(db, header[7:].strip())
    cookie = conn.cookies.get(SESSION_COOKIE)
    if cookie:
        return _principal_from_session(db, cookie)
    return None


# --------------------------------------------------------------------------- #
# Console tickets
# --------------------------------------------------------------------------- #
def mint_console_ticket(
    db: DbSession, principal: Principal, instance_id: str
) -> tuple[ConsoleTicket, str]:
    secret = generate_secret()
    row = ConsoleTicket(
        token_hash=hash_secret(secret),
        user_id=principal.user.id,
        instance_id=instance_id,
        session_id=principal.session_id,
        credential_version=principal.user.credential_version,
        expires_at=_utcnow() + CONSOLE_TICKET_TTL,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row, secret


def redeem_console_ticket(
    db: DbSession, secret: str, instance_id: str
) -> Principal | None:
    """Consume a ticket for exactly one instance, exactly once.

    Every rejection returns None rather than distinguishing why: a caller
    holding a wrong ticket learns only that it did not work.
    """
    row = db.exec(
        select(ConsoleTicket).where(ConsoleTicket.token_hash == hash_secret(secret))
    ).first()
    if row is None:
        return None
    # Bound to the instance. A ticket for one VM must not open another's screen.
    if row.instance_id != instance_id:
        logger.warning(
            "Console ticket for instance %s presented for %s", row.instance_id, instance_id
        )
        return None
    if row.used_at is not None or _aware(row.expires_at) <= _utcnow():
        return None

    user = db.get(User, row.user_id)
    if user is None or user.credential_version != row.credential_version:
        # A password change since minting kills the ticket, the same way it
        # kills every session and token.
        return None
    # Bound to its issuer: a ticket must not outlive the session that minted it,
    # or logging out would leave a working key to the console lying around.
    if row.session_id is not None and db.get(Session, row.session_id) is None:
        return None

    row.used_at = _utcnow()
    db.add(row)
    db.commit()
    return Principal(user=user, via="session", session_id=row.session_id)


def purge_expired(db: DbSession) -> int:
    """Drop expired sessions and spent tickets. Housekeeping, not security —
    the checks above already refuse them."""
    now = _utcnow()
    removed = 0
    for row in db.exec(select(Session)).all():
        if _aware(row.expires_at) <= now:
            db.delete(row)
            removed += 1
    for row in db.exec(select(ConsoleTicket)).all():
        if row.used_at is not None or _aware(row.expires_at) <= now:
            db.delete(row)
            removed += 1
    if removed:
        db.commit()
    return removed


# --------------------------------------------------------------------------- #
# The dependency every protected route carries
# --------------------------------------------------------------------------- #
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_user(
    conn: HTTPConnection,
    db: DbSession = Depends(get_session),
) -> Principal:
    """Authenticate the request, and enforce CSRF on cookie-authenticated writes.

    Applied to the whole application rather than route by route — see
    ``kurukuru.main`` — because the failure mode of the alternative is a route added
    later that nobody remembered to decorate.
    """
    principal = resolve_principal(conn, db)
    if principal is None:
        raise _UNAUTHENTICATED

    method = conn.scope.get("method", "")
    if principal.via == "session" and method in _UNSAFE_METHODS:
        presented = conn.headers.get(CSRF_HEADER, "")
        if not presented or not secrets_equal(presented, principal.csrf_token or ""):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Missing or invalid CSRF token. State-changing requests "
                    f"authenticated by cookie must send the {CSRF_HEADER} header."
                ),
            )
    return principal
