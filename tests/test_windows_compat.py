"""Windows specifics: exe suffixes, hook command quoting, schtasks plan.

Runs on any OS via monkeypatching sys.platform — both branches (win32 and
posix) are pinned down explicitly so the suite is host-independent: on a
real Windows runner the "posix" tests monkeypatch the platform the same way
the "windows" tests do on a Mac/Linux host.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import pytest

from skillmem import cli as C
from skillmem import schedule as SCHED


def test_venv_script_posix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(C.sys, "platform", "linux")
    assert C._venv_script("skillmem-mcp").name == "skillmem-mcp"


def test_venv_script_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(C.sys, "platform", "win32")
    assert C._venv_script("skillmem-mcp").name == "skillmem-mcp.exe"


def test_hook_cmd_posix_quotes_spaces(monkeypatch: pytest.MonkeyPatch):
    # PurePosixPath: a plain Path() on a Windows host becomes WindowsPath and
    # rewrites the separators to backslashes before _hook_cmd ever sees them.
    monkeypatch.setattr(C.sys, "platform", "linux")
    cmd = C._hook_cmd(PurePosixPath("/opt/skill mem/bin/skillmem"),
                      ["hook", "auto-recall"])
    assert cmd == "'/opt/skill mem/bin/skillmem' hook auto-recall"


def test_hook_cmd_windows_uses_double_quotes(monkeypatch: pytest.MonkeyPatch):
    """cmd.exe does not understand shlex single quotes — list2cmdline is required."""
    monkeypatch.setattr(C.sys, "platform", "win32")
    cmd = C._hook_cmd(Path(r"C:\Users\First Last\tools\skillmem.exe"), ["migrate"])
    assert cmd == r'"C:\Users\First Last\tools\skillmem.exe" migrate'
    assert "'" not in cmd


def test_schedule_backend_selection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(SCHED.sys, "platform", "win32")
    assert SCHED._backend()[0] is SCHED._schtasks_install
    monkeypatch.setattr(SCHED.sys, "platform", "darwin")
    assert SCHED._backend()[0] is SCHED._launchd_install
    # linux without a user systemd instance but with crontab -> cron
    monkeypatch.setattr(SCHED, "_systemd_available", lambda: False)
    monkeypatch.setattr(SCHED.shutil, "which", lambda name: "/usr/bin/crontab")
    monkeypatch.setattr(SCHED.sys, "platform", "linux")
    assert SCHED._backend()[0] is SCHED._cron_install
    # linux with a working `systemctl --user` -> systemd timers
    monkeypatch.setattr(SCHED, "_systemd_available", lambda: True)
    assert SCHED._backend()[0] is SCHED._systemd_install
    # an unknown platform degrades to the linux detection instead of crashing
    monkeypatch.setattr(SCHED, "_systemd_available", lambda: False)
    monkeypatch.setattr(SCHED.sys, "platform", "freebsd14")
    assert SCHED._backend()[0] is SCHED._cron_install


def test_schedule_jobs_use_exe_on_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(SCHED.sys, "platform", "win32")
    jobs = SCHED._jobs()
    assert jobs["decay"][0].endswith("skillmem.exe")
    assert jobs["decay"][1:] == ["decay", "--days", "14"]
    assert jobs["export"][1] == "export-all"


def test_win_tr_quotes_paths_with_spaces():
    tr = SCHED._win_tr([r"C:\Users\First Last\skillmem.exe", "decay", "--days", "14"])
    assert tr == r'"C:\Users\First Last\skillmem.exe" decay --days 14'


def test_launchd_plist_roundtrip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """On macOS, install writes valid plists carrying our jobs."""
    if sys.platform != "darwin":
        pytest.skip("launchd only on macOS")
    import plistlib
    calls: list[list[str]] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(SCHED.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or
                        __import__("types").SimpleNamespace(returncode=0))
    done = SCHED._launchd_install()
    assert len(done) == 2
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.skillmem.decay.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["ProgramArguments"][1:] == ["decay", "--days", "14"]
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 15}
    assert any("load" in c for cmd in calls for c in cmd)
