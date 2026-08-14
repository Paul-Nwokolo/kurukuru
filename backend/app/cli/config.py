"""
Where the CLI thinks the API is.

Resolution order, highest first:

1. ``--api-url``
2. ``IAAS_API_URL``
3. ``~/.local-iaas/cli.toml``
4. ``http://127.0.0.1:8000``

The order is the usual one for a reason: the flag is this invocation, the
environment is this shell, the file is this machine, the default is this
project. Each layer overrides a broader one.

Reading TOML needs no dependency — ``tomllib`` is in the standard library from
Python 3.11, which is the floor the backend already sets.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from app.cli.errors import CliError, ExitCode
from app.cli.naming import (
    CONFIG_PATH,
    DEFAULT_API_URL,
    DEFAULT_DASHBOARD_URL,
    env_var,
)


@dataclass(frozen=True)
class CliConfig:
    """Resolved settings for one invocation."""

    api_url: str
    dashboard_url: str
    #: Where each value came from, for ``doctor`` and for error messages that
    #: have to explain *why* the CLI was looking at that URL.
    api_url_source: str = "default"
    #: Project to scope commands to, by name or id. None means every project,
    #: which is what the CLI did before projects existed and stays the default:
    #: a scoping flag that defaults to *narrow* would hide instances from a
    #: script that never asked to be scoped.
    project: str | None = None

    @property
    def config_file(self) -> Path:
        return config_file_path()


def config_file_path() -> Path:
    return Path(CONFIG_PATH).expanduser()


def _load_file() -> dict[str, object]:
    """Read ``cli.toml``, or an empty mapping if there isn't one.

    A malformed file is an error rather than a silent fallback: falling back to
    the default URL would send the user's commands somewhere they did not ask
    for, and the resulting "connection refused" would blame the wrong thing.
    """
    path = config_file_path()
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise CliError(
            f"{path} is not valid TOML: {exc}",
            ExitCode.USAGE,
            hint="Fix or delete the file — every setting in it has a default.",
        ) from exc
    except OSError as exc:
        raise CliError(f"Could not read {path}: {exc}", ExitCode.USAGE) from exc


def _normalise(url: str) -> str:
    """Trim the trailing slash so paths can be joined by concatenation."""
    return url.rstrip("/")


def load_config(
    api_url: str | None = None,
    dashboard_url: str | None = None,
    project: str | None = None,
) -> CliConfig:
    """Resolve configuration for this invocation.

    ``api_url``/``dashboard_url`` are the flag values, already merged with the
    environment by Click (which prefers an explicit flag over ``envvar``). What
    is left for this function is the file and the default.
    """
    file_values = _load_file()

    resolved_api = api_url
    source = "flag or environment"
    if not resolved_api:
        from_file = file_values.get("api_url")
        if from_file is not None:
            if not isinstance(from_file, str):
                raise CliError(
                    f"api_url in {config_file_path()} must be a string",
                    ExitCode.USAGE,
                )
            resolved_api, source = from_file, str(config_file_path())
    if not resolved_api:
        resolved_api, source = DEFAULT_API_URL, "default"

    resolved_dashboard = dashboard_url
    if not resolved_dashboard:
        from_file = file_values.get("dashboard_url")
        resolved_dashboard = from_file if isinstance(from_file, str) else None
    if not resolved_dashboard:
        resolved_dashboard = DEFAULT_DASHBOARD_URL

    resolved_project = project
    if not resolved_project:
        from_file = file_values.get("project")
        resolved_project = from_file if isinstance(from_file, str) else None

    return CliConfig(
        api_url=_normalise(resolved_api),
        dashboard_url=_normalise(resolved_dashboard),
        api_url_source=source,
        project=resolved_project or None,
    )


#: Documented in ``--help`` so the order is discoverable without the docs.
RESOLUTION_HELP = (
    f"API URL resolution order: --api-url, then ${env_var('API_URL')}, "
    f"then {CONFIG_PATH}, then {DEFAULT_API_URL}."
)


def color_disabled() -> bool:
    """Whether colour must be suppressed regardless of the output stream.

    ``NO_COLOR`` is honoured by its own convention: *set at all*, whatever the
    value, means no colour. https://no-color.org
    """
    return os.environ.get("NO_COLOR") is not None
