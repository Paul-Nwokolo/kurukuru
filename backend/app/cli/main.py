"""
The command-line interface: root app, global options, error rendering.

The CLI is a second client of the HTTP API — the dashboard's peer, not its
back door. Every command here goes through :mod:`app.cli.client`; nothing
imports a router, a model or an engine.

The audience is people who live in terminals, so two things are treated as
contracts rather than niceties: the exit-code table below, and the rule that
``--json`` puts nothing but JSON on stdout.
"""

from __future__ import annotations

import os
import sys
from typing import Annotated

import typer
from typer.core import TyperGroup

from app.cli import (
    commands_auth,
    commands_events,
    commands_images,
    commands_networks,
    commands_projects,
    commands_instances,
    commands_snapshots,
    commands_volumes,
    commands_system,
)
from app.cli.config import RESOLUTION_HELP, load_config
from app.cli.errors import CliError, ExitCode
from app.cli.naming import (
    CLI_NAME,
    CONFIG_PATH,
    DEFAULT_API_URL,
    PRODUCT_NAME,
    apply_legacy_env,
    env_var,
)
from app.cli.output import Output

#: Set this to get the full traceback out of an unexpected failure.
TRACEBACK_ENV = env_var("CLI_TRACEBACK")

_EPILOG = f"""\
Configuration is resolved in this order: --api-url, then ${env_var("API_URL")},
then {CONFIG_PATH} (key: api_url), then {DEFAULT_API_URL}.

Exit codes: 0 success · 1 failure · 2 usage · 3 API unreachable · 4 not found ·
5 conflict (wrong state) · 6 invalid request or capacity refusal · 7 timed out.

Every read command takes --json, which writes JSON to stdout and nothing else;
progress and warnings always go to stderr.
"""

class _ErrorHandlingGroup(TyperGroup):
    """Turns a :class:`CliError` into its exit code, wherever it was raised.

    The boundary lives in the group rather than in ``main`` so that the exit
    codes are a property of the *application*, not of the console script that
    happens to launch it. Tests drive the app directly through Click's runner
    and see exactly the codes a shell would.
    """

    def invoke(self, ctx: typer.Context) -> object:
        try:
            return super().invoke(ctx)
        except CliError as exc:
            Output().error(exc.message, exc.hint)
            raise typer.Exit(int(exc.code)) from exc


app = typer.Typer(
    cls=_ErrorHandlingGroup,
    name=CLI_NAME,
    help=f"Launch and manage local VMs through the {PRODUCT_NAME} API.",
    epilog=_EPILOG,
    no_args_is_help=True,
    add_completion=False,  # replaced by the `completion` command
    pretty_exceptions_enable=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

commands_instances.register(app)
commands_system.register(app)
commands_events.register(app)
app.add_typer(commands_auth.auth_app, name="auth")
app.add_typer(commands_images.images_app, name="images")
app.add_typer(commands_images.isos_app, name="isos")
app.add_typer(commands_snapshots.snapshots_app, name="snapshot")
app.add_typer(commands_projects.projects_app, name="projects")
app.add_typer(commands_volumes.volumes_app, name="volumes")
app.add_typer(commands_networks.net_app, name="net")


@app.callback()
def root(
    ctx: typer.Context,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            envvar=env_var("API_URL"),
            help=f"Base URL of the orchestrator API. {RESOLUTION_HELP}",
            show_default=False,
        ),
    ] = None,
    dashboard_url: Annotated[
        str | None,
        typer.Option(
            "--dashboard-url",
            envvar=env_var("DASHBOARD_URL"),
            help="Base URL of the web dashboard, used by `console`.",
            show_default=False,
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            envvar=env_var("PROJECT"),
            help=(
                "Scope commands to one project, by name or id. Organisational "
                "only — it filters what you see, not what you may do."
            ),
            show_default=False,
        ),
    ] = None,
) -> None:
    """Resolve configuration once, for whichever command runs."""
    ctx.obj = load_config(
        api_url=api_url, dashboard_url=dashboard_url, project=project
    )


def main() -> None:
    """Entry point for the ``kurukuru`` console script.

    Owns the failure boundary. A command signals failure by raising
    :class:`CliError`, which carries the exit code the scripting contract
    promises; nothing below this function calls ``sys.exit`` with a bare number,
    and nothing lets an httpx traceback reach the user — a stack of transport
    internals tells them nothing about the fact that the backend isn't running.
    """
    # Before anything reads the environment. The CLI resolves its token file and
    # its state root from environment variables directly — it is forbidden from
    # importing the backend's Settings — so the backend's shim never runs for it
    # and an operator's IAAS_STATE_DIR would be invisible here while working
    # perfectly well for the server. Two halves of one product disagreeing about
    # where the install lives is worse than either one being wrong.
    for legacy, current in apply_legacy_env():
        print(
            f"warning: {legacy} is deprecated and will stop being read in a "
            f"future release. Rename it to {current}.",
            file=sys.stderr,
        )

    # Registers the shell-completion classes, so a completion request arriving
    # in $_KURUKURU_COMPLETE is answered by the same implementation whose script
    # `kurukuru completion` prints.
    from typer._completion_classes import completion_init

    completion_init()

    # Write UTF-8, and never die on a character that cannot be encoded.
    #
    # On Windows, Python picks the *locale* encoding (cp1252 here) for any
    # stream that is not a real console — which includes a pipe and, less
    # obviously, the pty behind Git Bash, where every em dash in the help text
    # came out as a replacement character. JSON is unaffected either way:
    # json.dumps escapes non-ASCII, so --json output is pure ASCII whatever
    # this does. ``errors="replace"`` is the belt to the brace: a character the
    # terminal cannot take must not kill a command that has already done its
    # work.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        app(prog_name=CLI_NAME)
    except CliError as exc:
        Output().error(exc.message, exc.hint)
        sys.exit(int(exc.code))
    except KeyboardInterrupt:
        # Ctrl-C is a decision, not a crash. 130 is the shell's convention for
        # "terminated by SIGINT" and keeps `&&` chains behaving.
        Output().note("Interrupted.")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - the boundary must not leak a traceback
        if os.environ.get(TRACEBACK_ENV):
            raise
        Output().error(
            f"Unexpected {exc.__class__.__name__}: {exc}",
            f"This is a bug. Re-run with {TRACEBACK_ENV}=1 for the full traceback.",
        )
        sys.exit(int(ExitCode.FAILURE))


if __name__ == "__main__":  # pragma: no cover - `python -m app.cli.main`
    main()
