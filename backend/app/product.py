"""
What the product is called. One definition, imported by every surface.

The name is not settled, and it appears in three places that are easy to forget
about individually: the CLI's own help and error hints, the dashboard's copy
("run ``iaas auth init``"), and the documentation. A rename that misses one of
them produces instructions that name a command the user does not have.

This module exists so the backend does not have to import from ``app.cli`` to
find that out. The CLI is a *client* of the API; a dependency pointing that way
would be backwards, and ``app.config`` cannot hold the name either, because the
CLI is forbidden from importing it (``test_the_cli_never_reaches_past_the_api``).
So the name lives below both, in a module with no dependencies at all, and both
import downward.

``app.cli.naming`` re-exports :data:`CLI_NAME` so existing CLI imports are
unaffected; the dashboard receives it as a field on ``GET /auth/first-run``.
"""

from __future__ import annotations

#: The command users type. Referenced everywhere; never spelled inline.
CLI_NAME = "iaas"

#: Prefix for the product's environment variables (``IAAS_API_URL``, …). Shared
#: by the backend's settings and the CLI's, on purpose — one namespace for the
#: whole product rather than one per surface.
ENV_PREFIX = "IAAS_"

#: Distribution that ships the command.
DISTRIBUTION_NAME = "local-iaas"
