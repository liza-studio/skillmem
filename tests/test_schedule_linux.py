"""Linux scheduling: systemd user timers, cron fallback, no-scheduler error.

Every subprocess call is mocked — no real systemctl/crontab/launchctl is ever
invoked, so the suite behaves identically on macOS/Windows/Linux hosts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from skillmem import schedule as SCHED


def _proc(rc: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


@pytest.fixture
def linux_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(SCHED.sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path / "data"))
    return tmp_path


# --------------------------------------------------------------------------- #
# availability probe
# --------------------------------------------------------------------------- #

def test_systemd_available_without_systemctl_binary(monkeypatch):
    monkeypatch.setattr(SCHED.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        SCHED.subprocess, "run",
        lambda *a, **kw: pytest.fail("must not call subprocess without binary"),
    )
    assert SCHED._systemd_available() is False


def test_systemd_available_probe_returncodes(monkeypatch):
    monkeypatch.setattr(SCHED.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(SCHED.subprocess, "run", lambda *a, **kw: _proc(0))
    assert SCHED._systemd_available() is True
    monkeypatch.setattr(SCHED.subprocess, "run", lambda *a, **kw: _proc(1))
    assert SCHED._systemd_available() is False


def test_systemd_available_oserror(monkeypatch):
    monkeypatch.setattr(SCHED.shutil, "which", lambda name: "/usr/bin/systemctl")

    def boom(*a, **kw):
        raise OSError("exec failed")

    monkeypatch.setattr(SCHED.subprocess, "run", boom)
    assert SCHED._systemd_available() is False


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #

def test_linux_no_scheduler_is_clear_error(monkeypatch, linux_env):
    monkeypatch.setattr(SCHED, "_systemd_available", lambda: False)
    monkeypatch.setattr(SCHED.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException, match="no scheduler available"):
        SCHED._backend()


# --------------------------------------------------------------------------- #
# systemd install / remove / status
# --------------------------------------------------------------------------- #

def test_systemd_install_writes_units_and_enables(monkeypatch, linux_env):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        SCHED.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd)) or _proc(0),
    )
    done = SCHED._systemd_install()
    assert len(done) == 2

    unit_dir = linux_env / ".config" / "systemd" / "user"
    for unit in ("skillmem-decay", "skillmem-export"):
        assert (unit_dir / f"{unit}.service").exists()
        assert (unit_dir / f"{unit}.timer").exists()

    decay_srv = (unit_dir / "skillmem-decay.service").read_text()
    assert "Type=oneshot" in decay_srv
    assert "decay --days 14" in decay_srv
    export_srv = (unit_dir / "skillmem-export.service").read_text()
    assert "export-all" in export_srv

    decay_timer = (unit_dir / "skillmem-decay.timer").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in decay_timer
    assert "Persistent=true" in decay_timer
    export_timer = (unit_dir / "skillmem-export.timer").read_text()
    assert "OnCalendar=Sun *-*-* 04:30:00" in export_timer

    assert ["systemctl", "--user", "daemon-reload"] in calls
    enables = [c for c in calls if "enable" in c]
    assert len(enables) == 2
    assert all("--now" in c for c in enables)
    assert {c[-1] for c in enables} == {
        "skillmem-decay.timer", "skillmem-export.timer",
    }


def test_systemd_install_enable_failure_raises(monkeypatch, linux_env):
    def run(cmd, **kw):
        if "enable" in cmd:
            return _proc(1, stderr="Failed to connect to bus")
        return _proc(0)

    monkeypatch.setattr(SCHED.subprocess, "run", run)
    with pytest.raises(click.ClickException, match="skillmem-decay.timer"):
        SCHED._systemd_install()


def test_systemd_remove_deletes_units(monkeypatch, linux_env):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        SCHED.subprocess, "run",
        lambda cmd, **kw: calls.append(list(cmd)) or _proc(0),
    )
    SCHED._systemd_install()
    calls.clear()

    removed = SCHED._systemd_remove()
    assert removed == ["removed skillmem-decay", "removed skillmem-export"]
    unit_dir = linux_env / ".config" / "systemd" / "user"
    assert not list(unit_dir.glob("skillmem-*"))
    assert ["systemctl", "--user", "daemon-reload"] in calls
    disables = [c for c in calls if "disable" in c]
    assert len(disables) == 2


def test_systemd_remove_nothing_to_do(monkeypatch, linux_env):
    monkeypatch.setattr(SCHED.subprocess, "run", lambda cmd, **kw: _proc(0))
    assert SCHED._systemd_remove() == []


def test_systemd_status_reports_unit_and_active(monkeypatch, linux_env):
    monkeypatch.setattr(SCHED.subprocess, "run", lambda cmd, **kw: _proc(0))
    SCHED._systemd_install()
    lines = SCHED._systemd_status()
    assert lines == [
        "skillmem-decay.timer: unit=yes active=yes",
        "skillmem-export.timer: unit=yes active=yes",
    ]
    # is-active failing -> active=no, still no exception
    monkeypatch.setattr(SCHED.subprocess, "run", lambda cmd, **kw: _proc(3))
    assert SCHED._systemd_status()[0].endswith("active=no")


# --------------------------------------------------------------------------- #
# cron without a crontab binary
# --------------------------------------------------------------------------- #

def _no_crontab(*a, **kw):
    raise FileNotFoundError("crontab")


def test_cron_read_without_binary_returns_empty(monkeypatch):
    monkeypatch.setattr(SCHED.subprocess, "run", _no_crontab)
    assert SCHED._cron_read() == []


def test_cron_write_without_binary_raises_clickexception(monkeypatch):
    monkeypatch.setattr(SCHED.subprocess, "run", _no_crontab)
    with pytest.raises(click.ClickException, match="crontab binary not found"):
        SCHED._cron_write(["* * * * * true"])
