"""
What the product is called. One definition, imported by every surface.

The name appears in places that are easy to forget about individually: the
CLI's own help and error hints, the dashboard's copy ("run ``kurukuru auth
init``"), the on-disk layout, the environment-variable namespace, and the
documentation. A rename that misses one of them produces instructions that name
a command the user does not have, or — far worse — a default path that strands
an existing install's VMs.

This module exists so the backend does not have to import from ``kurukuru.cli`` to
find that out. The CLI is a *client* of the API; a dependency pointing that way
would be backwards, and ``kurukuru.config`` cannot hold the name either, because the
CLI is forbidden from importing it (``test_the_cli_never_reaches_past_the_api``).
So the names live below both, in a module with no dependencies at all, and both
import downward.

``kurukuru.cli.naming`` re-exports :data:`CLI_NAME` so existing CLI imports are
unaffected; the dashboard receives it as a field on ``GET /auth/first-run``.

**The ``LEGACY_*`` half is load-bearing, not history.** Everything this tool was
called before Phase 16 is still sitting in ``~/.local-iaas`` on every existing
install, still spelled in every ``IAAS_*`` variable an operator set, and still
recorded inside runtime files and database rows. :mod:`kurukuru.state_migration` and
:func:`apply_legacy_env` below read these constants to carry that install
across. They are deleted when the deprecation window closes, and not before.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("kurukuru.product")

#: The product, as a human reads it.
PRODUCT_NAME = "Kurukuru"

#: What follows the name wherever it is introduced. The mark is deliberately
#: not shown bare: there is a live Nintendo registration for "KURUKURU KURURIN"
#: in Class 009 (video game programs), and while a single-host hypervisor
#: control plane is not in that category, looking like it might be is a cost
#: with no upside. See docs/DECISIONS.md.
PRODUCT_TAGLINE = "local cloud infrastructure"

#: The command users type. Referenced everywhere; never spelled inline.
#:
#: ``kk`` is taken on PyPI and ``kuru`` collides with a disease name, so the
#: full word ships and ``alias kk=kurukuru`` is documented for anyone who wants
#: the short form. A short binary name is not something a package can take back.
CLI_NAME = "kurukuru"

#: Prefix for the product's environment variables (``KURUKURU_API_URL``, …).
#: Shared by the backend's settings and the CLI's, on purpose — one namespace
#: for the whole product rather than one per surface.
ENV_PREFIX = "KURUKURU_"

#: Distribution that ships the command.
DISTRIBUTION_NAME = "kurukuru"

#: Root of everything this tool keeps on disk, before ``~`` is expanded.
#: :mod:`kurukuru.config` re-exports it as ``DEFAULT_STATE_DIR`` and roots every
#: other path under it.
STATE_DIR = "~/.kurukuru"

#: The database's filename under :data:`STATE_DIR`.
DATABASE_LEAF = "kurukuru.db"

#: Header carrying the CSRF token on state-changing cookie-authenticated calls.
#:
#: A *protocol* constant rather than copy, and it lives here for the same reason
#: the command name does: both halves of the product spell it, and before Phase
#: 16 they each spelled it independently — ``kurukuru.auth`` and ``kurukuru.cli.client``,
#: four literals between them. Renaming the product changed one of the two and
#: the CLI's login started failing its own CSRF check. The CLI may not import
#: ``kurukuru.auth`` (it is an API client, not part of the control plane), so the one
#: definition has to sit below both, which is here.
CSRF_HEADER = "X-Kurukuru-CSRF"

#: What the API answers when a credential was **presented and rejected**, as
#: distinct from absent. Both are 401, and the difference decides what the user
#: should be told to do next: an absent credential means "sign in", a rejected
#: one means "that password is wrong" — and telling somebody whose password was
#: just refused to go and sign in is a loop with no exit.
#:
#: Here rather than in ``kurukuru.auth`` for the same reason as CSRF_HEADER: the
#: CLI has to recognise it and may not import the control plane.
CREDENTIALS_REJECTED = "Incorrect username or password"

#: Where the HTTP API is mounted. Everything the CLI and the dashboard call
#: lives under it; nothing else does.
#:
#: **It exists because the dashboard and the API were the same eight URLs.**
#: The dashboard has a client-side route per page — ``/images``, ``/volumes``,
#: ``/instances/{id}`` — and the API had a route with the identical path and
#: method for each one. Serving both from one origin, which is what packaging
#: means, made ``GET /images`` two different requests distinguishable only by an
#: ``Accept`` header nobody sets deliberately. Deciding what to return from a
#: header like that is how you get a tool that works in a browser and returns
#: HTML to ``curl``.
#:
#: So the API moved and the dashboard kept the readable paths, because the
#: dashboard's are the ones a person types and bookmarks. Defined here, below
#: both surfaces, for the same reason :data:`CSRF_HEADER` is.
API_PREFIX = "/api"


# --------------------------------------------------------------------------- #
# What this was called before Phase 16
# --------------------------------------------------------------------------- #
#: The old command name. Still referenced by the dashboard's name guard, which
#: has to fail on the *old* literal as well as the new one — a rename is only
#: finished when the string it replaced can no longer reappear.
LEGACY_CLI_NAME = "iaas"

#: The old display name.
LEGACY_PRODUCT_NAME = "Local IaaS Orchestrator"

#: The old environment-variable prefix. Honoured for one release with a
#: deprecation warning; see :func:`apply_legacy_env` below for why
#: honouring beats refusing and why silently ignoring is not an option.
LEGACY_ENV_PREFIX = "IAAS_"

#: The old distribution name on PyPI.
LEGACY_DISTRIBUTION_NAME = "local-iaas"

#: Where an existing install's VMs, keys, ISOs, backups and database are right
#: now. :mod:`kurukuru.state_migration` moves this to :data:`STATE_DIR` once.
LEGACY_STATE_DIR = "~/.local-iaas"

#: The database's old filename inside that directory.
LEGACY_DATABASE_LEAF = "iaas.db"


def apply_legacy_env(environ: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Map surviving ``IAAS_*`` variables onto their ``KURUKURU_*`` names.

    Returns the ``(old, new)`` pairs it acted on, so a caller can report them.

    **Why honour them rather than refuse.** The three options were refuse,
    ignore, and honour-with-warning. Ignoring is the only one that is actually
    dangerous: an operator who set ``IAAS_STATE_DIR=D:/vms`` would find the
    backend silently pointed at ``~/.kurukuru`` instead, reporting an install
    with no instances in it — the "indistinguishable from data loss" failure
    that decision 26 exists to prevent, reintroduced by a rename. Refusing to
    start is safe but hostile: it turns an upgrade into an outage for the users
    who configured the tool most carefully. Honouring keeps their install
    working and tells them exactly what to change.

    ``IAAS_STATE_DIR`` in particular has to be read *before* the state-dir
    migration runs, because it is what says where this install actually lives.
    A migration that could not see it would look at an empty ``~/.local-iaas``,
    find nothing, and create a fresh empty tree beside a full one.

    **The new name always wins.** If both are set, the ``IAAS_`` one is ignored
    and reported — that is someone part-way through the rename, and preferring
    the value they just wrote is the only reading that is not surprising.
    """
    env = os.environ if environ is None else environ
    applied: list[tuple[str, str]] = []
    for old in [key for key in env if key.startswith(LEGACY_ENV_PREFIX)]:
        new = ENV_PREFIX + old[len(LEGACY_ENV_PREFIX):]
        if new in env:
            logger.warning(
                "%s and %s are both set; using %s. Remove %s.", old, new, new, old
            )
            continue
        env[new] = env[old]
        applied.append((old, new))
    return applied
