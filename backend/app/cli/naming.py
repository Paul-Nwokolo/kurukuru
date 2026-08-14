"""
What the command is called, in one place.

The product name is not final. Every other module asks for :data:`CLI_NAME`
rather than spelling it, so renaming the tool is this file plus the
``[project.scripts]`` entry in ``pyproject.toml`` — two lines, not a grep across
help text, error hints and documentation strings.

The environment-variable prefix travels with the name for the same reason, and
because it is already the backend's prefix (``IAAS_``): one namespace for the
whole product rather than one per surface.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _package_version

#: The command users type. Referenced everywhere; never spelled inline.
CLI_NAME = "iaas"

#: Prefix for the CLI's own environment variables (``IAAS_API_URL``, …).
#: Shared with the backend's ``pydantic-settings`` prefix on purpose — the
#: backend ignores keys it doesn't declare, so the two cannot collide.
ENV_PREFIX = "IAAS_"

#: Distribution that ships the command; the source of ``<cli> version``.
DISTRIBUTION_NAME = "local-iaas"

#: Where a user's persistent CLI settings live. Under the same directory the
#: backend already owns (keys, images, instances), so there is one place to look.
CONFIG_PATH = "~/.local-iaas/cli.toml"

#: Last resort when nothing else says where the API is.
DEFAULT_API_URL = "http://127.0.0.1:8000"

#: The Vite dev server's default. Only used to open a console in a browser.
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:5173"


def env_var(suffix: str) -> str:
    """Full name of one of our environment variables, e.g. ``IAAS_API_URL``."""
    return f"{ENV_PREFIX}{suffix.upper()}"


#: Reported when the package metadata is missing — a source tree that was never
#: installed. Deliberately not the backend's ``app_version``: that is the *API's*
#: version, which the CLI reads over HTTP and which need not be the same number
#: on a host the CLI is only talking to.
UNINSTALLED_VERSION = "0+source"


def cli_version() -> str:
    """Version of the installed distribution, or a marker if it isn't one."""
    try:
        return _package_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNINSTALLED_VERSION
