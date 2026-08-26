"""
``kurukuru serve`` — run the backend and the dashboard on one port.

**This module is the one part of the CLI allowed to import the backend**, and it
has a file of its own so that the exception is a filename a reviewer can see
rather than a line buried among the API-client commands.

Every other command here is a *client*: it reaches the system only over HTTP,
and ``test_the_cli_never_reaches_past_the_api`` enforces that by parsing imports.
``serve`` is categorically different — it does not call the backend, it *is* the
backend, started in this process. It has to know which interface and port the
backend is configured for, because uvicorn binds the socket before the
application loads and can therefore never be told by the application.

The alternative was to duplicate the two defaults and read the environment by
hand, the way ``auth_store`` duplicates the state directory. That was rejected
here: it would silently ignore a port set in ``backend/.env``, which the backend
itself honours — so the CLI and the backend would disagree about where the
service is, which is worse than an honest, narrow exception.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
from pathlib import Path
from typing import Annotated

import typer

from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.naming import CLI_NAME, env_var
from kurukuru.cli.output import Output


def register(app: typer.Typer) -> None:
    app.command("serve")(serve)


def serve(
    host: Annotated[str | None, typer.Option("--host", help="Interface to bind.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Port to bind.")] = None,
    reload: Annotated[
        bool, typer.Option("--reload", help="Restart on source changes (development).")
    ] = False,
) -> None:
    """Run the backend and the dashboard, in the foreground, on one port.

    A convenience wrapper around uvicorn, not a process manager: no daemonising,
    no PID file, no restart policy. Stopping it is Ctrl-C.

    Binds loopback by default, and moving off it is a deliberate act. Everything
    this serves is either unauthenticated at the network layer or protected by a
    session cookie over plain HTTP -- VM consoles, SSH forwards, the whole API --
    so a wildcard bind publishes all of it to the LAN. Passing --host says you
    accept that; the warning says what you accepted.
    """
    import uvicorn

    import kurukuru as package

    settings = _serve_settings()
    host = host or settings.host
    port = port if port is not None else settings.port

    # The backend resolves its SQLite URL and its .env against the working
    # directory. Started from anywhere else it would create a second, empty
    # database and report no instances -- so the directory is pinned to the
    # package's own, exactly as the documented uvicorn invocation assumes.
    backend_dir = Path(package.__file__).resolve().parent.parent
    out = Output()

    if not _is_loopback(host):
        out.warn(
            f"Binding {host}, not loopback. Every VM console, SSH forward and "
            f"API route on this host becomes reachable from the network, over "
            f"plain HTTP with no transport encryption. Put it behind a "
            f"TLS-terminating proxy, or bind 127.0.0.1 and tunnel to it."
        )

    _refuse_if_port_unusable(host, port)

    out.note(f"Serving {backend_dir} on http://{host}:{port} (Ctrl-C to stop)")
    os.chdir(backend_dir)

    try:
        uvicorn.run("kurukuru.main:app", host=host, port=port, reload=reload)
    except OSError as exc:
        # The pre-flight probe closes almost every case; this catches the race
        # where something takes the port between the probe and the real bind.
        raise _port_error(host, port, exc) from exc


def _serve_settings():
    """The backend's settings, imported late and only here.

    The CLI is forbidden from importing the backend's ``Settings`` -- it is an
    API client, and a test enforces the boundary. ``serve`` is the one command
    that is not acting as a client: it *is* the backend, started in this
    process, so it is allowed to know how the backend is configured. The import
    lives inside the function so that the exception belongs to the one function
    that needs it, rather than sitting at module scope where the next command
    added here would inherit it by accident.
    """
    from kurukuru.config import get_settings

    return get_settings()


def _is_loopback(host: str) -> bool:
    """Whether binding ``host`` keeps the service off the network.

    The wildcards mean "every interface", and the empty string means the same
    thing to the socket layer. Everything else is classified properly rather
    than string-matched, so ``127.0.0.2`` and ``::1`` are recognised for what
    they are instead of being warned about.
    """
    if host in ("", "0.0.0.0", "::"):  # noqa: S104 - detecting, not binding
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname we cannot classify; assume it is routable


def _refuse_if_port_unusable(host: str, port: int) -> None:
    """Fail with a sentence rather than a traceback when the port will not bind.

    Worth checking before uvicorn starts, because uvicorn's own failure is an
    OSError from inside asyncio that names neither the port nor what to do.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        raise _port_error(host, port, exc) from exc
    finally:
        probe.close()


def _port_error(host: str, port: int, exc: OSError) -> CliError:
    """Turn a bind failure into an explanation.

    **Two different failures wear the same shape on Windows**, and telling them
    apart is most of the value here.

    ``EADDRINUSE`` means something is listening: stop it, or pick another port.

    ``WSAEACCES`` (winerror 10013) means the port is *reserved* -- Windows'
    dynamic port range starts at 1024 on a default install, and Hyper-V and WSL
    reserve blocks inside it that move across reboots. So a port with nothing
    whatsoever listening on it can still refuse to bind. Reported as a bare
    permission error that reads as "run me as administrator", which is wrong,
    does not work, and sends the user somewhere unhelpful.
    """
    reserved = getattr(exc, "winerror", None) == 10013 or (
        exc.errno == errno.EACCES and exc.errno != errno.EADDRINUSE
    )
    alternative = port + 1
    if reserved:
        cause = (
            f"Port {port} is reserved on this machine, so nothing can bind it. "
            f"This is not the same as the port being in use, and it is not a "
            f"permissions problem -- elevating will not help. Windows reserves "
            f"blocks of ports for Hyper-V and WSL, and they move across reboots."
        )
        hint = (
            f"List the reserved blocks:\n"
            f"    netsh int ipv4 show excludedportrange protocol=tcp\n"
            f"then pick a port outside them:\n"
            f"    {CLI_NAME} serve --port {alternative}\n"
            f"or set {env_var('PORT')} to change the default."
        )
    else:
        cause = f"Something is already listening on {host}:{port}."
        hint = (
            f"Stop it, or serve somewhere else:\n"
            f"    {CLI_NAME} serve --port {alternative}\n"
            f"or set {env_var('PORT')} to change the default."
        )
    return CliError(cause, ExitCode.USAGE, hint=hint)
