"""
Command-line interface tests.

The transport is FastAPI's ``TestClient``, injected into the CLI's HTTP client.
That means every test here runs the *real* application — routes, validators,
background provisioning, the lot — with no server to start and no port to race
on, while still exercising the CLI as a pure API client. A command that reached
into the database instead of the API would go unnoticed by a mock; here it would
be reaching into a database that has nothing in it.

Three things are treated as contracts and tested as such:

* the exit-code table (a script branches on it),
* ``--json`` putting JSON and *only* JSON on stdout,
* not-a-TTY behaviour: no prompt that could hang a pipeline, no colour.
"""

from __future__ import annotations

import json
import sys

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
from typer.testing import CliRunner

import kurukuru.auth as auth
import kurukuru.engines as engines_module
from tests.conftest import api_client, authenticate_test_client, redirect_db_engines
import kurukuru.events as events_module
import kurukuru.routers.images as images_module
import kurukuru.routers.instances as instances_module
import kurukuru.routers.keypairs as keypairs_module
from kurukuru.cli import support
from kurukuru.cli.client import ApiClient, use_client
from kurukuru.cli.commands_instances import _ssh_command
from kurukuru.cli.errors import CliError, ExitCode
from kurukuru.cli.formats import parse_disk_gb, parse_memory_mb
from kurukuru.cli.main import app as cli_app
from kurukuru.config import Settings, get_settings
from kurukuru.database import get_session
from kurukuru.engines import EngineRegistry, InstanceInfo, get_engine_registry
from kurukuru.host_capacity import invalidate_cache
from kurukuru.main import app as api_app
from kurukuru.models import InstanceStatus
from tests.test_instances_api import FakeQemuEngine

runner = CliRunner()


class StuckEngine(FakeQemuEngine):
    """A hypervisor that accepts a launch and then never finishes it.

    Provisioning that never completes is the case ``--wait`` exists for, and
    the only way to prove the timeout path returns its own exit code rather
    than hanging or reporting success.
    """

    def provision_instance(self, name, cpus, memory, disk, cloud_init_path=None, options=None):
        self._record("provision", name)
        self.state[name] = InstanceInfo(
            name=name,
            exists=True,
            status=InstanceStatus.PROVISIONING,
            ip_address=None,
        )


class FailingEngine(FakeQemuEngine):
    """A hypervisor that refuses the launch, as a bad flag or a full disk would."""

    def provision_instance(self, name, cpus, memory, disk, cloud_init_path=None, options=None):
        from kurukuru.engines import ComputeEngineError

        raise ComputeEngineError("qemu-img: Could not create disk: No space left on device")


@pytest.fixture()
def make_cli(monkeypatch, tmp_path, small_host):
    """Build a CLI harness around a fresh in-memory application.

    Returns a callable so a test can choose the fake engine it needs; the
    default is the well-behaved one.
    """

    def _factory(engine=None):
        test_engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(test_engine)

        fake = engine or FakeQemuEngine()
        registry = EngineRegistry({"qemu": lambda: fake})

        def _session_override():
            with Session(test_engine) as session:
                yield session

        # Background jobs (provisioning, image import) and the startup hook that
        # registers the built-in image reach for the module-level DB engine and
        # the process-wide registry directly, not through DI.
        redirect_db_engines(monkeypatch, test_engine)
        monkeypatch.setattr(engines_module, "_registry", registry)

        iso_dir = tmp_path / "isos"
        iso_dir.mkdir(exist_ok=True)
        # state_dir is the one that matters: every _ROOTED_DIRS field this
        # fixture does NOT name explicitly (cloud_init_dir, db_backup_dir,
        # database_url, auth_token_file) is otherwise left at its default —
        # the real ~/.kurukuru — since Settings() with no state_dir override
        # resolves everything else against DEFAULT_STATE_DIR. Found leaking a
        # real, empty ~/.kurukuru/cloud-init directory (DECISIONS #59) because
        # this fixture named only the three fields the tests at the time
        # happened to touch, the exact hand-maintained-list mistake
        # conftest.py's own docstring warns isolated_state was built to
        # replace. iso_dir/qemu_dir/ssh_key_dir stay explicit below anyway —
        # _apply_state_dir only re-roots a field still at its default, so an
        # explicit value here is never overridden by adding state_dir.
        test_settings = Settings(
            state_dir=str(tmp_path / "state"),
            post_launch_ip_timeout_seconds=1,
            post_launch_poll_seconds=0.001,
            iso_dir=str(iso_dir),
            qemu_dir=str(tmp_path / "qemu"),
            ssh_key_dir=str(tmp_path / "keys"),
        )
        monkeypatch.setattr(instances_module, "get_settings", lambda: test_settings)
        monkeypatch.setattr(images_module, "get_settings", lambda: test_settings)

        api_app.dependency_overrides[get_session] = _session_override
        api_app.dependency_overrides[get_engine_registry] = lambda: registry
        api_app.dependency_overrides[get_settings] = lambda: test_settings

        # Polling is a real sleep; wind it down so the wait paths are exercised
        # in milliseconds rather than seconds.
        monkeypatch.setattr(support, "POLL_SECONDS", 0.01)
        invalidate_cache()
        http = api_client(api_app)
        http.app_engine = test_engine  # type: ignore[attr-defined]
        # The CLI talks to the API over this transport, so the credential
        # goes on the transport. `kurukuru auth ...` has its own tests.
        authenticate_test_client(http, test_engine)
        return http, fake

    yield _factory
    api_app.dependency_overrides.clear()
    invalidate_cache()


@pytest.fixture()
def cli(make_cli):
    """Invoke the CLI against a default application. Returns ``run(args)``."""
    http, _ = make_cli()

    def _run(*args: str, **kwargs):
        with http, use_client(http):
            return runner.invoke(cli_app, list(args), **kwargs)

    _run.http = http  # type: ignore[attr-defined]
    return _run


def launch(run, name: str = "web-one", *extra: str):
    result = run("launch", name, "--wait", *extra)
    assert result.exit_code == 0, result.output
    return result


# --------------------------------------------------------------------------- #
# Happy paths, one per command
# --------------------------------------------------------------------------- #
def test_launch_without_wait_reports_the_accepted_record(cli):
    """202 semantics are mirrored: an id comes back, not a finished VM."""
    result = cli("launch", "web-one")
    assert result.exit_code == 0
    assert "web-one" in result.stdout
    # The API answers Pending; provisioning happens after the response.
    assert "Pending" in result.stdout


def test_launch_with_wait_reaches_running(cli):
    result = launch(cli)
    assert "Running" in result.stdout
    assert "kurukuru ssh web-one" in result.stdout


def test_ls_lists_the_instance(cli):
    launch(cli)
    result = cli("ls")
    assert result.exit_code == 0
    assert "web-one" in result.stdout
    assert "Running" in result.stdout


def test_ls_hides_terminated_until_all(cli):
    launch(cli, "goner")
    cli("rm", "goner", "--yes")

    assert "goner" not in cli("ls").stdout
    assert "goner" in cli("ls", "--all").stdout


def test_show_reports_the_runtime_detail(cli):
    launch(cli)
    result = cli("show", "web-one")
    assert result.exit_code == 0
    for expected in ("ssh port", "vnc port", "accel", "display", "console"):
        assert expected in result.stdout


def test_stop_then_start(cli):
    launch(cli)
    stopped = cli("stop", "web-one")
    assert stopped.exit_code == 0
    assert "Stopped" in stopped.stdout

    started = cli("start", "web-one", "--wait")
    assert started.exit_code == 0
    assert "Running" in started.stdout


def test_rm_terminates(cli):
    launch(cli)
    result = cli("rm", "web-one", "--yes")
    assert result.exit_code == 0
    assert "Terminated" in result.stdout


def test_rm_is_idempotent_on_an_already_terminated_instance(cli):
    launch(cli, "twice")
    cli("rm", "twice", "--yes")
    again = cli("rm", "twice", "--yes")
    assert again.exit_code == 0
    assert "already terminated" in again.stderr


def test_capacity_shows_the_arithmetic(cli):
    result = cli("capacity")
    assert result.exit_code == 0
    for column in ("TOTAL", "COMMITTED", "ALLOCATABLE"):
        assert column in result.stdout


def test_isos_ls_on_an_empty_directory_explains_itself(cli):
    result = cli("isos", "ls")
    assert result.exit_code == 0
    assert "no boot media" in result.stdout
    # The remedy is placing files by hand; there is no upload endpoint.
    assert "KURUKURU_ISO_DIR" in result.stderr


def test_images_ls_shows_the_builtin_catalog_entry(cli):
    # The built-in image is registered by the app's lifespan, which TestClient
    # runs on entry.
    result = cli("images", "ls")
    assert result.exit_code == 0
    assert "Ubuntu" in result.stdout


def test_images_import_accepts_the_request_and_returns(cli, tmp_path):
    """202 semantics again: the copy happens in the background, like the API."""
    source = tmp_path / "mine.qcow2"
    source.write_bytes(b"not a real image")

    result = cli("images", "import", str(source), "--name", "Mine")
    assert result.exit_code == 0
    assert "Mine" in result.stdout


def test_images_rm_surfaces_the_api_refusal(cli):
    """The built-in image is not the user's to delete; the API says so."""
    catalog = json.loads(cli("images", "ls", "--json").stdout)
    builtin = next((i for i in catalog if i["source"] == "builtin"), None)
    if builtin is None:  # pragma: no cover - only if the lifespan hook changed
        pytest.skip("no built-in image registered")

    result = cli("images", "rm", builtin["name"], "--yes")
    assert result.exit_code == ExitCode.CONFLICT
    assert "built-in image cannot be deleted" in result.stderr


def test_console_prints_a_dashboard_deep_link(cli):
    launch(cli)
    result = cli("console", "web-one", "--print")
    assert result.exit_code == 0
    instance_id = json.loads(cli("show", "web-one", "--json").stdout)["id"]
    assert f"?console={instance_id}" in result.stdout


def test_version_reports_all_three(cli):
    result = cli("version", "--json")
    payload = json.loads(result.stdout)
    assert set(payload) == {"cli", "api", "qemu"}
    assert payload["cli"]
    assert payload["api"]


def test_completion_prints_a_script_for_a_known_shell(cli):
    result = cli("completion", "bash")
    assert result.exit_code == 0
    assert "_KURUKURU_COMPLETE" in result.stdout


def test_completion_rejects_an_unknown_shell(cli):
    result = cli("completion", "tcsh")
    assert result.exit_code == ExitCode.USAGE


# --------------------------------------------------------------------------- #
# The exit-code table
# --------------------------------------------------------------------------- #
def test_unknown_name_exits_not_found(cli):
    result = cli("show", "does-not-exist")
    assert result.exit_code == ExitCode.NOT_FOUND
    assert "ls --all" in result.stderr


def test_wrong_state_exits_conflict_with_the_api_message(cli):
    """The API's 409 already names the blocking state; it is printed verbatim."""
    launch(cli, "already-up")
    result = cli("start", "already-up")
    assert result.exit_code == ExitCode.CONFLICT
    assert "Running" in result.stderr


def test_capacity_refusal_exits_invalid_and_keeps_the_arithmetic(cli):
    result = cli("launch", "too-big", "--memory", "900G")
    assert result.exit_code == ExitCode.INVALID
    # The refusal spells out the numbers; substituting a generic message here
    # would throw away the only text that explains the limit.
    assert "allocatable" in result.stderr


def test_unreachable_api_exits_three_without_a_traceback():
    """No live server, no injected transport: a real connection is attempted."""
    with use_client(None):
        result = runner.invoke(
            cli_app, ["--api-url", "http://127.0.0.1:9", "ls"]
        )
    assert result.exit_code == ExitCode.UNREACHABLE
    assert "Cannot reach the API at http://127.0.0.1:9" in result.stderr
    assert "kurukuru serve" in result.stderr
    assert "Traceback" not in result.output
    assert "httpx" not in result.output


def test_wait_timeout_exits_seven(make_cli):
    http, _ = make_cli(StuckEngine())
    with http, use_client(http):
        result = runner.invoke(
            cli_app, ["launch", "stuck", "--wait", "--timeout", "0"]
        )
    assert result.exit_code == ExitCode.TIMEOUT
    assert "Timed out" in result.stderr
    # A timeout is this command giving up, not the instance being destroyed.
    assert "did not stop anything" in result.stderr


def test_failed_provisioning_exits_one_with_the_reason(make_cli):
    http, _ = make_cli(FailingEngine())
    with http, use_client(http):
        result = runner.invoke(cli_app, ["launch", "doomed", "--wait"])
    assert result.exit_code == ExitCode.FAILURE
    assert "No space left on device" in result.stderr


def test_unknown_option_is_a_usage_error(cli):
    assert cli("ls", "--nonsense").exit_code == ExitCode.USAGE


def test_watch_and_json_together_are_refused(cli):
    result = cli("ls", "--watch", "--json")
    assert result.exit_code == ExitCode.USAGE
    assert "incompatible" in result.stderr


def test_mode_iso_without_an_iso_is_a_usage_error(cli):
    result = cli("launch", "no-media", "--mode", "iso")
    assert result.exit_code == ExitCode.USAGE
    assert "--iso" in result.stderr


# --------------------------------------------------------------------------- #
# The scripting contract
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "command",
    [
        ("ls", "--json"),
        ("capacity", "--json"),
        ("images", "ls", "--json"),
        ("isos", "ls", "--json"),
        ("version", "--json"),
    ],
)
def test_json_output_is_only_json(cli, command):
    """Nothing but the document may reach stdout — the pipe has to parse."""
    result = cli(*command)
    assert result.exit_code == 0
    json.loads(result.stdout)  # raises if anything else was printed


def test_json_launch_keeps_progress_off_stdout(cli):
    """The spinner and its status lines are stderr, even while --wait polls."""
    result = cli("launch", "web-one", "--wait", "--json")
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["status"] == "Running"
    assert payload["name"] == "web-one"
    # The wait did report progress — to the other stream.
    assert "Launching web-one" in result.stderr
    assert "Launching" not in result.stdout


def test_json_show_matches_the_api_document(cli):
    launch(cli)
    from_cli = json.loads(cli("show", "web-one", "--json").stdout)
    from_api = cli.http.get(f"/instances/{from_cli['id']}").json()
    # The CLI reshapes nothing: --json is the API's own document, so a reader
    # only needs one reference for both.
    assert from_cli == from_api


def test_output_has_no_ansi_escapes_when_not_a_terminal(cli):
    launch(cli)
    result = cli("ls")
    assert "\x1b[" not in result.stdout


def test_rm_refuses_to_prompt_when_stdout_is_not_a_terminal(cli):
    """A prompt a script cannot answer is a hang, so it is never asked."""
    launch(cli, "scripted")
    result = cli("rm", "scripted")
    assert result.exit_code == ExitCode.USAGE
    assert "not a terminal" in result.stderr
    assert "--yes" in result.stderr
    # And nothing was destroyed.
    assert "scripted" in cli("ls").stdout


def test_no_color_env_disables_colour(monkeypatch):
    from kurukuru.cli.config import color_disabled

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert color_disabled() is False
    monkeypatch.setenv("NO_COLOR", "")  # set at all, whatever the value
    assert color_disabled() is True


# --------------------------------------------------------------------------- #
# ssh
# --------------------------------------------------------------------------- #
def test_ssh_command_carries_the_key_and_the_forwarded_port(cli):
    launch(cli)
    with cli.http, use_client(cli.http):
        api = ApiClient("http://127.0.0.1")
        instance = api.instance(json.loads(cli("show", "web-one", "--json").stdout)["id"])
        command = _ssh_command(api, instance)

    assert command[0] == "ssh"
    assert "-i" in command  # every VM trusts only the orchestrator's key
    assert command[command.index("-p") + 1] == str(instance["ssh_port"])
    assert command[-1].endswith(f"@{instance['ip_address']}")
    # Recycled loopback ports make known_hosts a liability, not a defence.
    assert "StrictHostKeyChecking=no" in command
    assert "--strict" not in command


def test_ssh_discards_host_keys_via_the_posix_null_device(cli):
    """`/dev/null` on every platform, including Windows — measured, not assumed.

    ``os.devnull`` is "nul" on Windows, which a POSIX ssh build (Git Bash's,
    the common Windows dev shell) reads as an ordinary relative filename and
    writes a real ./nul file into whatever directory the user is standing in.
    Win32 OpenSSH understands "/dev/null", so the POSIX spelling is the only
    one correct in all three environments. See NULL_DEVICE for the measurements.
    """
    from kurukuru.cli.commands_instances import NULL_DEVICE

    launch(cli)
    with cli.http, use_client(cli.http):
        api = ApiClient("http://127.0.0.1")
        instance = next(i for i in api.instances() if i["name"] == "web-one")
        command = _ssh_command(api, instance)

    assert NULL_DEVICE == "/dev/null"
    assert f"UserKnownHostsFile={NULL_DEVICE}" in command
    # The bug this guards: a bare device name that is really a relative path.
    assert not any(opt.lower().endswith("userknownhostsfile=nul") for opt in command)


def test_ssh_strict_keeps_openssh_defaults(cli):
    """The bypass is a default, not a decision taken away from the user."""
    launch(cli)
    with cli.http, use_client(cli.http):
        api = ApiClient("http://127.0.0.1")
        instance = next(i for i in api.instances() if i["name"] == "web-one")
        command = _ssh_command(api, instance, strict=True)

    assert "StrictHostKeyChecking=no" not in command
    assert "-i" in command  # the key is still required either way


def test_ssh_refuses_console_only_instances_and_points_at_console(make_cli, tmp_path):
    """An ISO guest never receives a key, so ssh is refused with the way in."""
    from kurukuru.cli.errors import CliError

    iso_dir = tmp_path / "isos"
    iso_dir.mkdir(exist_ok=True)
    (iso_dir / "alpine.iso").write_bytes(b"not really an iso")

    fake = FakeQemuEngine()
    fake.iso_mode = True  # no key injection, no address, no SSH port
    http, _ = make_cli(fake)

    with http, use_client(http):
        result = runner.invoke(
            cli_app, ["launch", "installer", "--iso", "alpine.iso", "--wait"]
        )
        assert result.exit_code == 0, result.output
        api = ApiClient("http://127.0.0.1")
        instance = next(i for i in api.instances() if i["name"] == "installer")
        assert instance["boot_source"] == "iso"
        with pytest.raises(CliError) as caught:
            _ssh_command(api, instance)

    assert caught.value.code is ExitCode.CONFLICT
    assert "kurukuru console installer" in (caught.value.hint or "")


def test_ssh_preflight_accepts_a_guest_that_sends_a_banner():
    """A real socket, not a mock: the banner is the only honest ready signal."""
    import socket
    import threading

    from kurukuru.cli.commands_instances import wait_for_ssh_service
    from kurukuru.cli.output import Output

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _serve() -> None:
        conn, _ = server.accept()
        conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
        conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        assert wait_for_ssh_service(Output(), "127.0.0.1", port, 5.0) is True
    finally:
        thread.join(timeout=2)
        server.close()


def test_ssh_preflight_rejects_a_port_that_answers_without_a_banner():
    """The failure this exists for: accepted, then dropped.

    QEMU's forwarded port is accepted whether or not the guest is listening,
    and cloud-init restarting sshd produces exactly this — an accept followed
    by EOF, which ssh reports as "kex_exchange_identification: read:
    Connection aborted". A port check would sail straight through it.
    """
    import socket
    import threading

    from kurukuru.cli.commands_instances import wait_for_ssh_service
    from kurukuru.cli.output import Output

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        server.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                continue
            conn.close()  # accepted, then dropped: no banner

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        # Never blocks the connection outright — it gives up and lets ssh
        # produce its own, better diagnosis.
        assert wait_for_ssh_service(Output(), "127.0.0.1", port, 0.1) is False
    finally:
        stop.set()
        thread.join(timeout=2)
        server.close()


@pytest.mark.posix_logic
def test_ssh_replaces_the_process_on_posix(monkeypatch):
    """execvp, not a subprocess: ssh must own the tty directly.

    A passthrough that ran ssh as a child would break job control, swallow
    Ctrl-C, and report its own exit status instead of the remote command's.
    Buffers are flushed first because execvp never returns to do it.
    """
    from kurukuru.cli import commands_instances as module

    flushed: list[str] = []
    execed: list[list[str]] = []

    monkeypatch.setattr(module.os, "name", "posix")
    monkeypatch.setattr(module.os, "execvp", lambda f, a: execed.append(a))
    monkeypatch.setattr(module.sys.stdout, "flush", lambda: flushed.append("out"))
    monkeypatch.setattr(module.sys.stderr, "flush", lambda: flushed.append("err"))
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: pytest.fail("POSIX must exec, never spawn a child"),
    )

    # The real execvp never returns; the fake does, and the POSIX branch is
    # terminal rather than falling through into the Windows one.
    with pytest.raises(CliError):
        module._exec(["ssh", "-p", "2200", "iaas@127.0.0.1"])

    assert execed == [["ssh", "-p", "2200", "iaas@127.0.0.1"]]
    assert set(flushed) == {"out", "err"}


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="no execvp passthrough on Windows")
def test_ssh_runs_a_child_and_propagates_its_status_on_windows(monkeypatch):
    """Windows has no execve that survives a shell, so the status is relayed."""
    from kurukuru.cli import commands_instances as module

    class Completed:
        returncode = 42

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Completed())

    with pytest.raises(SystemExit) as caught:
        module._exec(["ssh", "whatever"])
    assert caught.value.code == 42


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
HEALTHY_DIAGNOSTICS = {
    "api": {"version": "0.1.0"},
    "python": "3.13.7",
    "engine": {
        "available": True,
        "version": "QEMU emulator version 10.0.94",
        "accel": "whpx",
        "accel_available": True,
        "base_image": "C:/base.img",
        "base_image_present": True,
    },
    "instance_store": {
        "path": "C:/instances",
        "exists": True,
        "writable": True,
        "free_bytes": 200 * 1024**3,
    },
    "ssh_key": {"present": True, "private_key_path": "C:/keys/id_ed25519", "error": None},
}


def test_doctor_passes_on_a_healthy_host(cli, monkeypatch):
    monkeypatch.setattr(ApiClient, "diagnostics", lambda self: HEALTHY_DIAGNOSTICS)
    result = cli("doctor")
    assert result.exit_code == 0
    assert "FAIL" not in result.stdout
    assert result.stdout.count("PASS") >= 6


def test_doctor_fails_and_prescribes_when_qemu_is_missing(cli, monkeypatch):
    broken = {
        **HEALTHY_DIAGNOSTICS,
        "engine": {"available": False, "error": "QEMU binary not found"},
    }
    monkeypatch.setattr(ApiClient, "diagnostics", lambda self: broken)
    result = cli("doctor")
    assert result.exit_code == ExitCode.FAILURE
    assert "FAIL QEMU" in result.stdout
    # A verdict without a remedy is not a diagnosis.
    assert "PATH" in result.stdout


def test_doctor_warns_but_passes_without_an_accelerator(cli, monkeypatch):
    slow = {
        **HEALTHY_DIAGNOSTICS,
        "engine": {**HEALTHY_DIAGNOSTICS["engine"], "accel_available": False, "accel": "tcg"},
    }
    monkeypatch.setattr(ApiClient, "diagnostics", lambda self: slow)
    result = cli("doctor")
    assert result.exit_code == 0  # slow is not broken
    assert "WARN Accelerator" in result.stdout


def test_doctor_exits_three_when_the_api_is_unreachable():
    with use_client(None):
        result = runner.invoke(cli_app, ["--api-url", "http://127.0.0.1:9", "doctor"])
    assert result.exit_code == ExitCode.UNREACHABLE
    assert "FAIL API" in result.stdout
    assert "kurukuru serve" in result.stdout


def test_doctor_json_is_a_list_of_checks(cli, monkeypatch):
    monkeypatch.setattr(ApiClient, "diagnostics", lambda self: HEALTHY_DIAGNOSTICS)
    checks = json.loads(cli("doctor", "--json").stdout)
    assert {"name", "status", "detail", "remedy"} == set(checks[0])
    assert all(check["status"] in ("PASS", "WARN", "FAIL") for check in checks)


# --------------------------------------------------------------------------- #
# The invariant this whole phase rests on
# --------------------------------------------------------------------------- #
def test_the_cli_never_reaches_past_the_api():
    """No database, no engines, no models, no routers — HTTP or nothing.

    The CLI ships *inside* the backend package, so a session or a SQLModel
    import is always one line away and would work perfectly on the developer's
    machine. It would also make the CLI silently wrong the moment the backend
    is somewhere else, and let a capability exist in the CLI that the dashboard
    (and every other client) cannot have. This is the guard that keeps a
    missing endpoint being fixed as a missing endpoint.

    ``serve`` is one exception, and only to locate the package it runs; it
    imports uvicorn and starts the app rather than driving it.

    ``host_admin.py`` is the other, added in Phase 15, and it is deliberately a
    whole file rather than a scattering of imports so the exception is one
    filename. It creates the first account and resets a forgotten password.
    Neither can be an API call: the first would need a public, state-changing
    route that hands ownership to whoever calls it first, and the second is the
    recovery path for someone with no credential, so it cannot require one.
    Both instead require filesystem access to the state directory, which is a
    *stronger* requirement — whoever has it can already read the orchestrator's
    SSH private key and every VM disk beside it (DECISIONS #47).
    """
    # Two files, each for a stated reason:
    #
    #   host_admin.py — creates the first account on the host, before any
    #     credential exists to authenticate an API call with (decision 47).
    #   serve.py      — does not call the backend, it *is* the backend, started
    #     in this process. uvicorn binds the socket before the application
    #     loads, so the bind address can never come from the application; the
    #     launcher has to read the same configuration the backend will.
    #
    # Both are whole files rather than exempted lines, so the exception is
    # visible in a directory listing rather than buried among client commands.
    exempt = {"host_admin.py", "serve.py"}
    import ast
    from pathlib import Path

    import kurukuru.cli as cli_package

    forbidden = (
        "kurukuru.models",
        "kurukuru.database",
        "kurukuru.engines",
        "kurukuru.routers",
        "kurukuru.host_capacity",
        "kurukuru.image_store",
        "kurukuru.isos",
        "kurukuru.ssh_keys",
        "kurukuru.config",
        "sqlmodel",
        "sqlalchemy",
    )

    offences: list[str] = []
    for source in sorted(Path(cli_package.__file__).parent.glob("*.py")):
        if source.name in exempt:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == bad or name.startswith(bad + ".") for bad in forbidden):
                    offences.append(f"{source.name}:{node.lineno} imports {name}")

    assert not offences, "the CLI must go through the HTTP API:\n" + "\n".join(offences)


# --------------------------------------------------------------------------- #
# Size parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [("2G", 2048), ("2048M", 2048), ("2048", 2048), ("512m", 512), ("1.5G", 1536)],
)
def test_memory_accepts_the_spellings_people_type(value, expected):
    assert parse_memory_mb(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("20G", 20), ("20", 20), ("20480M", 20), ("1500M", 2)],
)
def test_disk_rounds_up_to_whole_gigabytes(value, expected):
    """Rounding down would quietly hand over a smaller disk than was asked for."""
    assert parse_disk_gb(value) == expected


def test_a_size_that_is_not_a_size_is_a_usage_error():
    from kurukuru.cli.errors import CliError

    with pytest.raises(CliError) as caught:
        parse_memory_mb("plenty")
    assert caught.value.code is ExitCode.USAGE
    assert "2G" in (caught.value.hint or "")


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
def test_events_shows_one_instance_history_newest_first(cli):
    launch(cli, "web-one")
    cli("stop", "web-one")

    result = cli("events", "web-one")

    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    body = "\n".join(lines)
    assert "Stopped" in body and "Created from" in body
    # Newest first: the stop must appear above the creation.
    assert body.index("Stopped") < body.index("Created from")


def test_events_without_an_instance_shows_the_whole_feed(cli):
    launch(cli, "web-one")
    launch(cli, "web-two")

    result = cli("events")

    assert result.exit_code == 0
    assert "web-one" in result.stdout and "web-two" in result.stdout
    assert "INSTANCE" in result.stdout  # the column only appears in the global view


def test_events_json_emits_only_json(cli):
    launch(cli, "web-one")

    result = cli("events", "web-one", "--json")

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert {e["kind"] for e in payload} >= {"created", "provisioning_succeeded"}


def test_events_can_filter_by_kind(cli):
    launch(cli, "web-one")

    result = cli("events", "web-one", "--kind", "created", "--json")

    assert [e["kind"] for e in json.loads(result.stdout)] == ["created"]


def test_an_unknown_kind_is_a_usage_error_that_lists_the_real_ones(cli):
    result = cli("events", "--kind", "exploded")

    assert result.exit_code == ExitCode.USAGE
    assert "snapshot_restored" in result.output


def test_events_for_an_unknown_instance_exits_not_found(cli):
    result = cli("events", "nope")

    assert result.exit_code == ExitCode.NOT_FOUND


def test_a_terminated_instance_still_has_a_readable_history(cli):
    """The case the log exists for: the row is gone from `ls`, the history is
    not gone from the system."""
    launch(cli, "web-one")
    cli("rm", "web-one", "--yes")

    result = cli("events", "web-one", "--json")

    assert result.exit_code == 0
    assert "terminated" in [e["kind"] for e in json.loads(result.stdout)]


def test_the_cli_kind_list_matches_the_api_enum(cli):
    """Two copies of one vocabulary; this is what keeps them equal."""
    from kurukuru.cli.commands_events import KNOWN_KINDS
    from kurukuru.models import EventKind

    assert set(KNOWN_KINDS) == {k.value for k in EventKind}


# --------------------------------------------------------------------------- #
# Projects
# --------------------------------------------------------------------------- #
def test_projects_ls_shows_the_default(cli):
    result = cli("projects", "ls")

    assert result.exit_code == 0
    assert "default" in result.stdout


def test_projects_create_and_ls_json(cli):
    assert cli("projects", "create", "client-a", "-d", "billable").exit_code == 0

    payload = json.loads(cli("projects", "ls", "--json").stdout)

    assert {p["name"] for p in payload} == {"default", "client-a"}


def test_the_project_flag_scopes_ls(cli):
    """It filters the view. It is not a permission — the unscoped listing below
    still shows everything."""
    cli("projects", "create", "client-a")
    launch(cli, "web-one")
    # --project is a root option, like --api-url: it scopes the invocation, not
    # one subcommand.
    assert cli("--project", "client-a", "launch", "web-two", "--wait").exit_code == 0

    scoped = json.loads(cli("--project", "client-a", "ls", "--json").stdout)
    everything = json.loads(cli("ls", "--json").stdout)

    assert {i["name"] for i in scoped} == {"web-two"}
    assert {i["name"] for i in everything} == {"web-one", "web-two"}


def test_the_env_var_scopes_the_same_way(cli, monkeypatch):
    cli("projects", "create", "client-a")
    assert cli("--project", "client-a", "launch", "web-two", "--wait").exit_code == 0
    launch(cli, "web-one")

    monkeypatch.setenv("KURUKURU_PROJECT", "client-a")
    scoped = json.loads(cli("ls", "--json").stdout)

    assert {i["name"] for i in scoped} == {"web-two"}


def test_launching_while_scoped_files_the_instance_there(cli):
    cli("projects", "create", "client-a")

    assert cli("--project", "client-a", "launch", "web-one", "--wait").exit_code == 0

    projects = {p["name"]: p for p in json.loads(cli("projects", "ls", "--json").stdout)}
    assert projects["client-a"]["instance_count"] == 1
    assert projects["default"]["instance_count"] == 0


def test_an_unknown_project_exits_not_found(cli):
    assert cli("--project", "nope", "ls").exit_code == ExitCode.NOT_FOUND


def test_removing_a_project_with_live_instances_exits_conflict(cli):
    cli("projects", "create", "client-a")
    assert cli("--project", "client-a", "launch", "web-one", "--wait").exit_code == 0

    result = cli("projects", "rm", "client-a", "--yes")

    assert result.exit_code == ExitCode.CONFLICT
    assert "web-one" in result.output


def test_removing_an_empty_project_succeeds(cli):
    cli("projects", "create", "client-a")

    assert cli("projects", "rm", "client-a", "--yes").exit_code == 0
    assert "client-a" not in cli("projects", "ls").stdout


def test_project_rm_never_prompts_when_not_a_tty(cli):
    """The scripting contract: a prompt a pipeline cannot answer is a hang."""
    cli("projects", "create", "client-a")

    result = cli("projects", "rm", "client-a")

    assert result.exit_code == ExitCode.USAGE
    assert "client-a" in cli("projects", "ls").stdout  # nothing was deleted


# --------------------------------------------------------------------------- #
# Volumes
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vol_cli(cli, monkeypatch, tmp_path):
    """CLI harness whose volume creation writes a real (tiny) file."""
    from pathlib import Path

    from kurukuru.config import Settings
    from kurukuru.engines.qemu import QemuEngine
    import kurukuru.routers.volumes as volumes_module

    def _fake_create(self, path, size_gb):  # noqa: ANN001
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"QFI\xfb")

    monkeypatch.setattr(QemuEngine, "create_blank_disk", _fake_create)
    monkeypatch.setattr(
        volumes_module, "get_settings", lambda: Settings(state_dir=str(tmp_path / "vol"))
    )
    return cli


def test_volumes_create_and_ls(vol_cli):
    assert vol_cli("volumes", "create", "data", "5G", "--wait").exit_code == 0

    result = vol_cli("volumes", "ls")

    assert "data" in result.stdout and "5G" in result.stdout


def test_volume_attach_prints_the_guest_steps(vol_cli):
    """A raw device is useless until it is formatted, and nothing here does it
    for the user — so the commands that do have to be on screen."""
    vol_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_cli, "web-one")
    vol_cli("stop", "web-one", "--wait")

    result = vol_cli("volumes", "attach", "data", "web-one")

    assert result.exit_code == 0
    assert "mkfs.ext4" in result.output and "lsblk" in result.output


def test_attaching_to_a_running_instance_exits_conflict(vol_cli):
    vol_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_cli, "web-one")

    result = vol_cli("volumes", "attach", "data", "web-one")

    assert result.exit_code == ExitCode.CONFLICT
    assert "corrupts a mounted filesystem" in result.output


def test_removing_an_attached_volume_exits_conflict(vol_cli):
    vol_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_cli, "web-one")
    vol_cli("stop", "web-one", "--wait")
    vol_cli("volumes", "attach", "data", "web-one")

    result = vol_cli("volumes", "rm", "data", "--yes")

    assert result.exit_code == ExitCode.CONFLICT


def test_volume_rm_never_prompts_when_not_a_tty(vol_cli):
    vol_cli("volumes", "create", "data", "1G", "--wait")

    result = vol_cli("volumes", "rm", "data")

    assert result.exit_code == ExitCode.USAGE
    assert "data" in vol_cli("volumes", "ls").stdout  # nothing was deleted


def test_terminating_an_instance_leaves_its_volume_available(vol_cli):
    """The guarantee, through the CLI a user would actually type."""
    vol_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_cli, "web-one")
    vol_cli("stop", "web-one", "--wait")
    vol_cli("volumes", "attach", "data", "web-one")

    assert vol_cli("rm", "web-one", "--yes").exit_code == 0

    volumes = json.loads(vol_cli("volumes", "ls", "--json").stdout)
    assert [(v["name"], v["status"]) for v in volumes] == [("data", "Available")]


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #
def test_net_modes_names_the_elevation_each_deferred_mode_needs(cli):
    """The useful answer to "why can't I bridge?" — a driver and a privilege,
    per platform, not an apology."""
    result = cli("net", "modes")

    assert result.exit_code == 0
    assert "Administrator" in result.stdout
    assert "tap-windows6" in result.stdout
    assert "CAP_NET_ADMIN" in result.stdout


def test_net_ls_shows_the_default_user_network(cli):
    result = cli("net", "ls")

    assert result.exit_code == 0
    assert "user" in result.stdout


def test_forwards_lists_ssh_as_derived(cli):
    launch(cli, "web-one")

    result = cli("net", "forwards", "web-one")

    assert result.exit_code == 0
    assert "derived" in result.stdout


def test_forward_and_unforward(cli):
    launch(cli, "web-one")

    assert cli("net", "forward", "web-one", "18080", "80").exit_code == 0
    rows = json.loads(cli("net", "forwards", "web-one", "--json").stdout)
    assert [r["host_port"] for r in rows if not r["derived"]] == [18080]

    assert cli("net", "unforward", "web-one", "18080").exit_code == 0
    rows = json.loads(cli("net", "forwards", "web-one", "--json").stdout)
    assert [r for r in rows if not r["derived"]] == []


def test_unforwarding_the_ssh_port_is_refused_by_the_cli(cli):
    """It is not removable, so the CLI must not offer to try — and the hint
    says why rather than reporting a bare 'not found'."""
    instance = launch(cli, "web-one")
    ssh_port = json.loads(cli("show", "web-one", "--json").stdout)["ssh_port"]

    result = cli("net", "unforward", "web-one", str(ssh_port))

    assert result.exit_code == ExitCode.NOT_FOUND
    assert "cannot be removed" in result.output


def test_a_colliding_forward_exits_invalid_with_the_reason(cli):
    launch(cli, "web-one")
    cli("net", "forward", "web-one", "18080", "80")

    result = cli("net", "forward", "web-one", "18080", "8080")

    assert result.exit_code == ExitCode.INVALID
    assert "already forwards" in result.output


# --------------------------------------------------------------------------- #
# Volume snapshots — a separate command tree from `kurukuru snapshot`
# --------------------------------------------------------------------------- #
@pytest.fixture()
def vol_snap_cli(vol_cli):
    """vol_cli is enough: FakeQemuEngine already implements volume snapshots in
    memory, so nothing here needs to reach for qemu-img."""
    return vol_cli


def test_volume_snapshot_create_and_ls(vol_snap_cli):
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")

    created = vol_snap_cli(
        "volumes", "snapshot", "create", "data", "before-upgrade", "--wait"
    )
    assert created.exit_code == 0, created.output

    listed = vol_snap_cli("volumes", "snapshot", "ls", "data")
    assert listed.exit_code == 0
    assert "before-upgrade" in listed.stdout


def test_volume_snapshot_create_says_the_instance_is_not_included(vol_snap_cli):
    """The half of the warning this command owns. Said at the moment of the
    action, not only in a help string."""
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")

    result = vol_snap_cli("volumes", "snapshot", "create", "data", "snap", "--wait")

    assert "not any instance" in result.output


def test_a_volume_attached_to_a_stopped_instance_can_be_snapshotted(vol_snap_cli):
    """The decision the feature turns on: attached is fine, running is not."""
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_snap_cli, "web-one")
    vol_snap_cli("stop", "web-one", "--wait")
    assert vol_snap_cli("volumes", "attach", "data", "web-one").exit_code == 0

    result = vol_snap_cli("volumes", "snapshot", "create", "data", "attached", "--wait")

    assert result.exit_code == 0, result.output


def test_snapshotting_while_a_running_instance_holds_it_exits_conflict(vol_snap_cli):
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")
    launch(vol_snap_cli, "web-one")
    vol_snap_cli("stop", "web-one", "--wait")
    vol_snap_cli("volumes", "attach", "data", "web-one")
    vol_snap_cli("start", "web-one", "--wait")

    result = vol_snap_cli("volumes", "snapshot", "create", "data", "nope")

    assert result.exit_code == ExitCode.CONFLICT
    assert "mid-write" in result.output
    assert "can stay attached" in result.output


def test_volume_snapshot_restore_and_rm(vol_snap_cli):
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")
    vol_snap_cli("volumes", "snapshot", "create", "data", "before", "--wait")

    restored = vol_snap_cli("volumes", "snapshot", "restore", "data", "before", "--yes")
    assert restored.exit_code == 0, restored.output

    removed = vol_snap_cli(
        "volumes", "snapshot", "rm", "data", "before", "--yes", "--wait"
    )
    assert removed.exit_code == 0, removed.output
    assert "no snapshots" in vol_snap_cli("volumes", "snapshot", "ls", "data").stdout


def test_an_unknown_volume_snapshot_exits_not_found(vol_snap_cli):
    vol_snap_cli("volumes", "create", "data", "1G", "--wait")

    result = vol_snap_cli("volumes", "snapshot", "restore", "data", "ghost", "--yes")

    assert result.exit_code == ExitCode.NOT_FOUND
    assert "no snapshot named" in result.output


# --------------------------------------------------------------------------- #
# kurukuru auth — first run, sign in, tokens
# --------------------------------------------------------------------------- #
@pytest.fixture()
def auth_cli(cli, tmp_path, monkeypatch):
    """The CLI harness with its own token file, and no token in it yet.

    `cli` authenticates its transport directly (every other test needs that);
    these tests are about the credential the CLI resolves for itself, so the
    header is removed and the token file redirected somewhere disposable.
    """
    from kurukuru.cli import auth_store

    token_file = tmp_path / "cli-token"
    monkeypatch.setattr(auth_store, "token_path", lambda settings=None: token_file)
    # host_admin talks to the database directly (it is the one CLI module allowed
    # to). Point it at this test's engine, or `auth init` would read the real
    # one — the developer's ~/.kurukuru/kurukuru.db — and answer from it.
    import kurukuru.database as database_module

    monkeypatch.setattr(database_module, "engine", cli.http.app_engine)  # type: ignore[attr-defined]
    monkeypatch.setattr(database_module, "init_db", lambda: None)
    cli.http.headers.pop("Authorization", None)
    cli.token_file = token_file  # type: ignore[attr-defined]
    return cli


def _interactive(passwords):
    """Patch the prompt, and claim a terminal so the password path runs."""
    from unittest.mock import patch

    import typer as _typer

    from kurukuru.cli.output import Output

    values = list(passwords)
    return (
        patch.object(Output, "interactive", property(lambda self: True)),
        patch.object(_typer, "prompt", side_effect=values if len(values) > 1
                     else lambda *a, **k: values[0]),
    )


def test_a_command_without_a_credential_says_how_to_sign_in(auth_cli):
    result = auth_cli("ls")

    assert result.exit_code == ExitCode.UNAUTHENTICATED
    assert "Not authenticated" in result.output
    assert "auth login" in result.output
    assert "auth init" in result.output


def test_auth_login_stores_a_token_and_unlocks_the_cli(auth_cli):
    interactive, prompt = _interactive(["test-password-1234"])
    with interactive, prompt:
        result = auth_cli("auth", "login")

    assert result.exit_code == 0, result.output
    assert "Signed in as owner" in result.output
    assert auth_cli.token_file.exists()
    # And the CLI now works without any further help.
    assert auth_cli("auth", "whoami").exit_code == 0


def test_the_stored_token_is_not_the_password(auth_cli):
    interactive, prompt = _interactive(["test-password-1234"])
    with interactive, prompt:
        auth_cli("auth", "login")

    contents = auth_cli.token_file.read_text()
    assert "test-password-1234" not in contents
    assert auth.TOKEN_PREFIX in contents


def test_a_wrong_password_is_refused_generically(auth_cli):
    interactive, prompt = _interactive(["wrong-password-here"])
    with interactive, prompt:
        result = auth_cli("auth", "login")

    assert result.exit_code == ExitCode.UNAUTHENTICATED
    assert "Incorrect username or password" in result.output
    assert not auth_cli.token_file.exists()


def test_login_refuses_when_stdin_is_not_a_terminal(auth_cli):
    """A password must never come from a flag or a pipe by accident — that is
    how it ends up in shell history and in this project's approved-command
    list, which is the leak CONTRIBUTING documents."""
    result = auth_cli("auth", "login")

    assert result.exit_code == ExitCode.USAGE
    assert "not a terminal" in result.output


def test_logout_revokes_the_token_and_removes_the_file(auth_cli):
    interactive, prompt = _interactive(["test-password-1234"])
    with interactive, prompt:
        auth_cli("auth", "login")
    stored = auth_cli.token_file.read_text()

    assert auth_cli("auth", "logout").exit_code == 0

    assert not auth_cli.token_file.exists()
    # Revoked server-side too, not merely forgotten locally.
    import json

    secret = json.loads(stored)["token"]
    auth_cli.http.headers["Authorization"] = f"Bearer {secret}"
    assert auth_cli.http.get("/instances").status_code == 401


def test_token_create_prints_the_secret_once(auth_cli):
    interactive, prompt = _interactive(["test-password-1234"])
    with interactive, prompt:
        auth_cli("auth", "login")

    result = auth_cli("auth", "token", "create", "ci-runner")

    assert result.exit_code == 0
    assert auth.TOKEN_PREFIX in result.output
    # A second look never shows it again.
    listed = auth_cli("auth", "token", "ls")
    assert auth.TOKEN_PREFIX in listed.output   # the prefix is shown
    assert result.output.strip().splitlines()[0] not in listed.output


def test_token_rm_revokes(auth_cli):
    interactive, prompt = _interactive(["test-password-1234"])
    with interactive, prompt:
        auth_cli("auth", "login")
    auth_cli("auth", "token", "create", "ci-runner")

    removed = auth_cli("auth", "token", "rm", "ci-runner", "--yes")

    assert removed.exit_code == 0
    assert "revoked" in auth_cli("auth", "token", "ls").output


def test_init_refuses_when_an_account_already_exists(auth_cli):
    interactive, prompt = _interactive(["another-long-password"] * 2)
    with interactive, prompt:
        result = auth_cli("auth", "init")

    assert result.exit_code == ExitCode.CONFLICT
    assert "already has an account" in result.output


def test_the_cli_and_the_backend_agree_on_the_token_path(monkeypatch):
    """The one value duplicated across the CLI/backend boundary.

    ``auth_store`` cannot import ``kurukuru.config`` — that is the boundary the test
    above enforces — so the token file's default location is written down twice.
    Duplication is the right trade there, but only if something notices when the
    two drift, because the symptom otherwise is the CLI writing a token the
    backend never reads and a login that appears to succeed and changes nothing.
    """
    from kurukuru.cli import auth_store
    from kurukuru.config import Settings

    # The suite isolates this via the environment; the comparison is about the
    # built-in defaults, so the override has to come off first.
    monkeypatch.delenv("KURUKURU_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.delenv("KURUKURU_STATE_DIR", raising=False)

    assert Settings().auth_token_file == (
        f"{auth_store.DEFAULT_STATE_DIR}/{auth_store.TOKEN_LEAF}"
    )
    # And the re-rooting rule matches too.
    assert Settings(state_dir="/srv/iaas").auth_token_file == (
        f"/srv/iaas/{auth_store.TOKEN_LEAF}"
    )
