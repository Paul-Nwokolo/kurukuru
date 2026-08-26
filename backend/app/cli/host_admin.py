"""The two operations that run on the host instead of through the API.

Everything else in ``app/cli`` goes over HTTP, and ``test_the_cli_never_reaches
_past_the_api`` enforces that — a CLI that can reach the database directly would
work on the developer's machine, break the moment the backend is elsewhere, and
give the CLI a capability no other client can have. This module is a named
exception to that rule, in the same way ``serve`` is, and it is kept in its own
file so the exception is one filename rather than a scattering of imports.

**Why creating the first account is not an API call.** It would need a public,
state-changing route: reachable without a credential, callable exactly once, and
whoever calls it becomes the owner. On a non-loopback bind that is a race for
ownership of the machine's VMs. Doing it on the host instead requires filesystem
access to the state directory — a *stronger* requirement than any credential
this product could check, because whoever has it can already read the
orchestrator's SSH private key and every VM disk sitting beside it. So the local
path protects more and adds no public surface.

Password reset is here for the same reason and one more: it is the recovery path
for someone who has lost the credential, so it cannot itself require one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OwnerCreated:
    username: str
    token: str


class HostAdminError(RuntimeError):
    """Something the caller should turn into a CliError with a hint."""


def _session():
    """A session on the *current* engine.

    ``app.database.engine`` is read at call time rather than bound at import.
    That is not style: the module-level engine is created from settings when the
    package is first imported, so a `from app.database import engine` here would
    capture the developer's real database and keep using it even after a test
    redirected the module attribute — which is how a test suite ends up writing
    an account into ~/.kurukuru/kurukuru.db.
    """
    from sqlmodel import Session

    import app.database as database

    # A fresh install has no tables yet: `kurukuru auth init` may genuinely be the
    # first thing that ever touches this database.
    database.init_db()
    return Session(database.engine)


def account_exists() -> bool:
    from sqlmodel import select

    from app.models import User

    with _session() as session:
        return session.exec(select(User)).first() is not None


def create_owner(username: str, password: str, token_label: str) -> OwnerCreated:
    """Create the owner account and mint its first API token.

    Refuses if any account exists. Re-running this must never be a way to add a
    second owner or to overwrite the first one's password — that is what
    :func:`reset_password` is for, and it is a separate, louder command.
    """
    from sqlmodel import select

    from app import auth
    from app.models import User

    with _session() as session:
        if session.exec(select(User)).first() is not None:
            raise HostAdminError("This install already has an account.")
        user = User(
            username=username,
            password_hash=auth.hash_password(password),
            is_owner=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        _row, secret = auth.create_api_token(session, user, token_label)
        return OwnerCreated(username=user.username, token=secret)


def reset_password(username: str, password: str) -> None:
    """Set a new password without the old one, and invalidate everything."""
    from sqlmodel import select

    from app import auth
    from app.models import User

    with _session() as session:
        user = session.exec(select(User).where(User.username == username)).first()
        if user is None:
            raise HostAdminError(f"No account named '{username}'.")
        user.password_hash = auth.hash_password(password)
        # Sessions and tokens all die with the old password — including the one
        # in this machine's token file, which the caller then clears.
        auth.invalidate_credentials(session, user)


def validate_password(password: str) -> str:
    """The API's own rule, applied before writing rather than after."""
    from app.models import validate_password as _validate

    return _validate(password)


def minimum_password_length() -> int:
    from app.models import MIN_PASSWORD_LENGTH

    return MIN_PASSWORD_LENGTH
