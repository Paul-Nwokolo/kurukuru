"""
What the command is called, in one place.

The product name is not final. Every other module asks for :data:`CLI_NAME`
rather than spelling it, so renaming the tool is :mod:`kurukuru.product` plus the
``[project.scripts]`` entry in ``pyproject.toml`` — two lines, not a grep across
help text, error hints and documentation strings.

The name and the environment-variable prefix are defined in :mod:`kurukuru.product`
and re-exported here. They are shared with the backend, which needs the command
name to tell the dashboard what to tell the user to run, and a module below both
surfaces is the only place a shared name can live without one importing the
other. Everything else here is the CLI's alone.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _package_version

# The names themselves live in ``kurukuru.product``, which has no dependencies, so
# the backend can import them without depending on its own client package. They
# are re-exported here because every CLI module already asks this module for
# them, and the indirection is not worth a hundred-line diff.
from kurukuru.product import (
    API_PREFIX,
    CLI_NAME,
    CSRF_HEADER,
    DISTRIBUTION_NAME,
    ENV_PREFIX,
    PRODUCT_NAME,
    apply_legacy_env,
)

__all__ = [
    "API_PREFIX",
    "CLI_NAME",
    "CSRF_HEADER",
    "PRODUCT_NAME",
    "apply_legacy_env",
    "CONFIG_PATH",
    "DEFAULT_API_URL",
    "DEFAULT_DASHBOARD_URL",
    "DISTRIBUTION_NAME",
    "ENV_PREFIX",
    "UNINSTALLED_VERSION",
    "cli_version",
    "env_var",
]

#: Where a user's persistent CLI settings live. Under the same directory the
#: backend already owns (keys, images, instances), so there is one place to look.
CONFIG_PATH = "~/.kurukuru/cli.toml"

#: Last resort when nothing else says where the API is. An *origin*: the
#: API's mount point is appended by ``ApiClient``, in one place.
DEFAULT_API_URL = "http://127.0.0.1:7842"

#: Where the dashboard is. Only used to open a console in a browser.
#: The same origin as the API now, because the backend serves the dashboard
#: itself — it is a separate setting only so that a development session
#: pointing at the Vite dev server can still say so.
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:7842"


def env_var(suffix: str) -> str:
    """Full name of one of our environment variables, e.g. ``KURUKURU_API_URL``."""
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
