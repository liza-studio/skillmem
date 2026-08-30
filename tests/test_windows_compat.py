"""Windows-специфика: exe-суффиксы, квотинг команд хуков, schtasks-план.

Прогоняется на любой ОС через monkeypatch sys.platform — реальный Windows
для CI недоступен, поэтому фиксируем хотя бы платформенное ветвление.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skillmem import cli as C
from skillmem import schedule as SCHED


def test_venv_script_posix():
    assert C._venv_script("skillmem-mcp").name == "skillmem-mcp"


def test_venv_script_windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(C.sys, "platform", "win32")
    assert C._venv_script("skillmem-mcp").name == "skillmem-mcp.exe"


def test_hook_cmd_posix_quotes_spaces():
    cmd = C._hook_cmd(Path("/opt/liza mem/bin/skillmem"), ["hook", "auto-recall"])
    assert cmd == "'/opt/liza mem/bin/skillmem' hook auto-recall"


def test_hook_cmd_windows_uses_double_quotes(monkeypatch: pytest.MonkeyPatch):
    """cmd.exe не понимает одинарные кавычки shlex — нужен list2cmdline."""
    monkeypatch.setattr(C.sys, "platform", "win32")
    cmd = C._hook_cmd(Path(r"C:\Users\First Last\liza\skillmem.exe"), ["migrate"])
    assert cmd == r'"C:\Users\First Last\liza\skillmem.exe" migrate'
    assert "'" not in cmd


def test_schedule_backend_selection(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(SCHED.sys, "platform", "win32")
    assert SCHED._backend()[0] is SCHED._schtasks_install
    monkeypatch.setattr(SCHED.sys, "platform", "darwin")
    assert SCHED._backend()[0] is SCHED._launchd_install
    monkeypatch.setattr(SCHED.sys, "platform", "linux")
    assert SCHED._backend()[0] is SCHED._cron_install
    # неизвестная платформа деградирует в cron, а не падает
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
    """На маке install пишет валидные plist'ы с нашими джобами."""
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
