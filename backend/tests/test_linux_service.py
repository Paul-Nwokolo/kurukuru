"""The systemd user unit and the steps that install it.

The unit text and every systemctl call are checked here with a fake runner,
so this runs on any OS. Whether systemd actually starts, restarts and survives
logout with it is the WSL live check (PORTABILITY, Phase 18) — these tests say
what is asked of systemd, not what systemd does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kurukuru import linux_service as svc


class FakeSystemctl:
    def __init__(self, fail_on: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command, **kwargs):
        assert kwargs.get("timeout"), "every systemctl call carries a timeout"
        assert isinstance(command, list), "an argument list, never a shell string"
        self.calls.append(command)
        failed = self.fail_on is not None and self.fail_on in command
        return subprocess.CompletedProcess(command, 1 if failed else 0,
                                           stdout="active\n", stderr="boom" if failed else "")


@pytest.fixture(autouse=True)
def no_real_home(tmp_path: Path, monkeypatch):
    """Every fallback to the home directory lands in tmp_path.

    The first version of these tests wrote a unit file into the developer's
    real ~/.config/systemd/user — a "relative" XDG_CONFIG_HOME check that
    called a Windows path relative fell back to Path.home(). The fix is in
    unit_dir; this makes the class of mistake unable to reach a real home again.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config"
    assert svc.unit_dir({"XDG_CONFIG_HOME": str(config)}).is_relative_to(tmp_path)
    return {"XDG_CONFIG_HOME": str(config)}


def test_the_unit_keeps_vms_alive_across_a_restart():
    """systemd's default KillMode=control-group would kill every QEMU in the
    service's cgroup on stop, restart or upgrade. VMs are designed to outlive
    the backend; this is what lets them."""
    unit = svc.render_unit("/home/ada/.local/bin/kurukuru")
    assert "\nKillMode=process\n" in unit
    assert "\nExecStart=/home/ada/.local/bin/kurukuru serve\n" in unit
    assert "\nWantedBy=default.target\n" in unit
    assert "\nRestart=on-failure\n" in unit


def test_the_unit_needs_an_absolute_executable():
    """A user manager's PATH is not the login shell's; ~/.local/bin, where
    pipx puts kurukuru, is usually not on it."""
    with pytest.raises(svc.ServiceError, match="absolute path"):
        svc.render_unit("kurukuru")


def test_the_unit_goes_where_systemd_looks(env, no_real_home):
    assert svc.unit_dir(env) == Path(env["XDG_CONFIG_HOME"]) / "systemd" / "user"
    assert svc.unit_dir({"XDG_CONFIG_HOME": "relative"}) == no_real_home / ".config" / "systemd" / "user"


def test_install_writes_reloads_enables_and_restarts(env):
    fake = FakeSystemctl()
    plan = svc.install("/opt/kk/bin/kurukuru", environ=env, runner=fake)

    assert plan.unit_path.read_text(encoding="utf-8") == svc.render_unit("/opt/kk/bin/kurukuru")
    assert b"\r" not in plan.unit_path.read_bytes()
    assert fake.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", svc.UNIT_NAME],
        # restart, not start: after an upgrade the running backend is the old code.
        ["systemctl", "--user", "restart", svc.UNIT_NAME],
    ]


def test_reinstalling_is_idempotent_and_reports_a_change(env):
    svc.install("/opt/kk/bin/kurukuru", environ=env, runner=FakeSystemctl())
    again = svc.plan_install("/opt/kk/bin/kurukuru", environ=env)
    moved = svc.plan_install("/home/ada/.local/bin/kurukuru", environ=env)

    assert again.replaces and again.unchanged
    assert moved.replaces and not moved.unchanged


def test_a_failing_systemctl_step_says_what_systemctl_said(env):
    with pytest.raises(svc.ServiceError, match="boom"):
        svc.install("/opt/kk/bin/kurukuru", environ=env, runner=FakeSystemctl(fail_on="enable"))


def test_uninstall_removes_the_unit_and_nothing_else(env, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "kurukuru.db").write_bytes(b"precious")
    svc.install("/opt/kk/bin/kurukuru", environ=env, runner=FakeSystemctl())
    fake = FakeSystemctl()

    assert svc.uninstall(environ=env, runner=fake) is True
    assert not (svc.unit_dir(env) / svc.UNIT_NAME).exists()
    assert ["systemctl", "--user", "disable", "--now", svc.UNIT_NAME] in fake.calls
    assert (state / "kurukuru.db").read_bytes() == b"precious"


def test_uninstalling_nothing_is_not_an_error(env):
    fake = FakeSystemctl()
    assert svc.uninstall(environ=env, runner=fake) is False
    assert fake.calls == []


def test_no_systemctl_is_reported_as_no_systemd(env):
    def missing(command, **kwargs):
        raise FileNotFoundError(command[0])

    with pytest.raises(svc.ServiceError, match="does not run systemd"):
        svc.systemctl("daemon-reload", runner=missing)
