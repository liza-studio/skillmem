"""`skillmem init --claude-code` and uninstall round-trip."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from skillmem.cli import main as cli_main


def _run(args: list[str]) -> tuple[int, str]:
    """Invoke the CLI in-process; stderr is combined into output."""
    runner = CliRunner()
    result = runner.invoke(cli_main, args, catch_exceptions=False)
    return result.exit_code, result.output


def test_init_creates_mcp_entry_and_hook(fakehome: Path):
    """init --claude-code populates ~/.claude.json and adds Stop hook."""
    code, _ = _run(["init", "--claude-code", "--mcp-binary",
                    str(Path(sys.executable).parent / "skillmem-mcp"),
                    "--skip-migrate"])
    assert code == 0
    cfg = json.loads((fakehome / ".claude.json").read_text())
    assert "skillmem" in cfg["mcpServers"]
    settings = json.loads((fakehome / ".claude" / "settings.json").read_text())
    assert "Stop" in settings["hooks"]
    assert any("skillmem" in h["command"]
               for grp in settings["hooks"]["Stop"]
               for h in grp["hooks"])


def test_init_idempotent(fakehome: Path):
    """Second init must not duplicate the MCP entry or hooks."""
    args = ["init", "--claude-code", "--mcp-binary",
            str(Path(sys.executable).parent / "skillmem-mcp"),
            "--skip-migrate"]
    _run(args)
    settings1 = json.loads((fakehome / ".claude" / "settings.json").read_text())
    _run(args)
    cfg = json.loads((fakehome / ".claude.json").read_text())
    assert len(cfg["mcpServers"]) == 1
    settings2 = json.loads((fakehome / ".claude" / "settings.json").read_text())
    assert settings1["hooks"] == settings2["hooks"]
    # full mode: Stop = session-recap only. The Stop→migrate hook is gone: it
    # imported the alphabetically-first project's memory dir, not this one's.
    hook_count = sum(len(grp["hooks"]) for grp in settings2["hooks"]["Stop"])
    assert hook_count == 1
    # the deny rule for `skillmem trust` is installed once, not per run
    assert settings2["permissions"]["deny"].count("Bash(skillmem trust*)") == 1


def test_init_hooks_minimal(fakehome: Path):
    """--hooks minimal installs no hooks — only the `trust` deny rule."""
    _run(["init", "--claude-code", "--hooks", "minimal", "--mcp-binary",
          str(Path(sys.executable).parent / "skillmem-mcp"), "--skip-migrate"])
    settings = json.loads((fakehome / ".claude" / "settings.json").read_text())
    assert not settings.get("hooks")
    assert "Bash(skillmem trust*)" in settings["permissions"]["deny"]


def test_init_hooks_full_registers_all_events(fakehome: Path):
    """Default (full) wires SessionStart / UserPromptSubmit / PreToolUse / Stop."""
    _run(["init", "--claude-code", "--mcp-binary",
          str(Path(sys.executable).parent / "skillmem-mcp"), "--skip-migrate"])
    settings = json.loads((fakehome / ".claude" / "settings.json").read_text())
    events = set(settings["hooks"].keys())
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"} <= events
    pre = settings["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Bash|Edit|Write|NotebookEdit"
    recap = [h for g in settings["hooks"]["Stop"] for h in g["hooks"]
             if "session-recap" in h["command"]]
    assert recap and recap[0]["timeout"] == 95


def test_init_refuses_corrupted_existing_config(fakehome: Path):
    """If ~/.claude.json is invalid JSON, init must NOT overwrite — only back it up."""
    bad = fakehome / ".claude.json"
    bad.write_text("{this is not valid json")
    code, out = _run(["init", "--claude-code", "--mcp-binary",
                      str(Path(sys.executable).parent / "skillmem-mcp"),
                      "--skip-migrate"])
    assert code == 0
    # Output begins with the warn line, then the JSON report. Slice from first '{'.
    json_start = out.find("{\n")
    report = json.loads(out[json_start:].split("\n\nDone")[0])
    assert report["claude_json"]["changed"] is False
    # Original corrupt content preserved
    assert bad.read_text().startswith("{this is not")
    # A .bak file exists somewhere alongside
    assert list(fakehome.glob(".claude.json.bak.*"))


def test_uninstall_restores_clean_config(fakehome: Path):
    """uninstall removes the MCP entry and hook, leaves DB intact."""
    _run(["init", "--claude-code", "--mcp-binary",
          str(Path(sys.executable).parent / "skillmem-mcp"),
          "--skip-migrate"])
    _run(["uninstall", "--keep-db"])
    cfg = json.loads((fakehome / ".claude.json").read_text())
    assert "mcpServers" not in cfg or "skillmem" not in cfg.get("mcpServers", {})
    settings = json.loads((fakehome / ".claude" / "settings.json").read_text())
    # The Stop hook list should be gone, or contain no skillmem entries.
    for grp in settings.get("hooks", {}).get("Stop", []):
        for h in grp.get("hooks", []):
            assert "skillmem" not in h.get("command", "")


def test_mcp_subcommand_exists():
    """Registry clients launch `uvx skillmem mcp` — the subcommand must exist."""
    from click.testing import CliRunner
    from skillmem.cli import main as cli_main
    r = CliRunner().invoke(cli_main, ["mcp", "--help"])
    assert r.exit_code == 0
    assert "MCP" in r.output or "stdio" in r.output


def test_hooks_status_reads_the_log(tmp_path, monkeypatch):
    """Hooks swallow their own errors, so one that quietly stopped working looks
    like one with nothing to do. This command is where the difference shows."""
    from click.testing import CliRunner
    from skillmem.cli import main as cli_main
    log = tmp_path / "hooks.log"
    log.write_text(
        "2026-09-14T10:00:00\tauto-recall\tabcd1234\t42\t2\tslug-a,slug-b\t900\n"
        "2026-09-14T10:01:00\tsession-recap\tabcd1234\tskip:debounce 12s < 600s\n"
        "2026-09-14T10:02:00\tsession-recap\tabcd1234\twrote session-x.md (500b)\n",
        encoding="utf-8")
    monkeypatch.setenv("SKILLMEM_HOOK_LOG", str(log))
    monkeypatch.setenv("SKILLMEM_STATE_DIR", str(tmp_path / "state"))
    out = CliRunner().invoke(cli_main, ["hooks-status"], catch_exceptions=False)
    assert out.exit_code == 0
    assert "auto-recall" in out.output and "session-recap" in out.output
    assert "skipped=1" in out.output


def test_search_widens_past_a_wall_of_notes(tmp_path, monkeypatch):
    """Filtering a fixed window returned nothing while a matching skill sat just
    below it — the window has to widen instead."""
    from click.testing import CliRunner
    from skillmem import storage as S
    from skillmem.cli import main as cli_main
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path))
    monkeypatch.setenv("SKILLMEM_STATE_DIR", str(tmp_path / "state"))
    db = tmp_path / "memory.db"
    conn = S.connect(db)
    S.init_schema(conn)
    for i in range(60):                      # a wall of session recaps
        S.upsert(conn, S.MemoryItem(
            slug=f"session-2026-09-{i:02d}-aaaa", kind="note",
            title=f"выжимка {i} про хук рекапа",
            body="хук рекапа сработал, выжимка сессии про хук рекапа"))
    S.upsert(conn, S.MemoryItem(
        slug="skill-recap-hook", kind="skill", title="Хук рекапа: как починить",
        body="trigger: хук рекапа плодит сессии; steps: вернуть env-флаг."))
    conn.commit()
    out = CliRunner().invoke(cli_main, ["--db", str(db), "search", "хук рекапа", "--limit", "3"],
                             catch_exceptions=False)
    assert out.exit_code == 0
    assert "skill-recap-hook" in out.output


def test_init_rewrites_hooks_from_another_venv_instead_of_doubling(fakehome: Path):
    """A hook already wired to an older venv is repointed, not duplicated —
    a doubled Stop hook would recap every session twice."""
    mcp = str(Path(sys.executable).parent / "skillmem-mcp")
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    settings_json = fakehome / ".claude" / "settings.json"
    settings = json.loads(settings_json.read_text())
    cmds = lambda s: [h["command"] for g in s["hooks"].values() for grp in g for h in grp["hooks"]]
    before = cmds(settings)
    for grp in (g for lst in settings["hooks"].values() for g in lst):
        for h in grp["hooks"]:
            if h["command"].endswith("hook session-recap"):
                h["command"] = "/old/venv/bin/skillmem hook session-recap"
    settings["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "/old/venv/bin/skillmem hook foreign-thing"}]})
    settings_json.write_text(json.dumps(settings))
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    after = cmds(json.loads(settings_json.read_text()))
    assert len(after) == len(before) + 1                      # only the foreign hook is extra
    assert "/old/venv/bin/skillmem hook foreign-thing" in after  # untouched
    assert not any(c.endswith("/old/venv/bin/skillmem hook session-recap") for c in after)
    assert sum(c.endswith("hook session-recap") for c in after) == sum(c.endswith("hook session-recap") for c in before)


def _hook_cmds(settings):
    return [h["command"] for g in settings["hooks"].values() for grp in g for h in grp["hooks"]]


def test_init_repairs_an_already_doubled_install(fakehome: Path):
    """Two copies of the same hook (old venv + new, or old + old) collapse to one."""
    mcp = str(Path(sys.executable).parent / "skillmem-mcp")
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    settings_json = fakehome / ".claude" / "settings.json"
    settings = json.loads(settings_json.read_text())
    stop = settings["hooks"]["Stop"]
    stop.insert(0, {"hooks": [{"type": "command", "command": "/old/venv/bin/skillmem hook session-recap", "timeout": 10}]})
    stop.append({"hooks": [{"type": "command", "command": "/old2/venv/bin/skillmem hook session-recap"}]})
    settings_json.write_text(json.dumps(settings))
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    stop = json.loads(settings_json.read_text())["hooks"]["Stop"]
    recaps = [h["command"] for g in stop for h in g["hooks"] if h["command"].endswith("hook session-recap")]
    assert len(recaps) == 1 and "/old" not in recaps[0]     # SessionEnd keeps its own copy
    assert stop and all(g["hooks"] for g in stop)            # no empty groups left


def test_init_does_not_mistake_a_differently_scoped_hook_for_ours(fakehome: Path):
    """A tool-recall hook the user scoped to `Read` is neither repointed nor counted as present."""
    mcp = str(Path(sys.executable).parent / "skillmem-mcp")
    settings_json = fakehome / ".claude" / "settings.json"
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    settings_json.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Read", "hooks": [{"type": "command", "command": "/old/venv/bin/skillmem hook tool-recall"}]}]}}))
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    pre = json.loads(settings_json.read_text())["hooks"]["PreToolUse"]
    by_matcher = {g["matcher"]: g["hooks"][0]["command"] for g in pre}
    assert by_matcher["Read"] == "/old/venv/bin/skillmem hook tool-recall"
    assert by_matcher["Bash|Edit|Write|NotebookEdit"].endswith("skillmem hook tool-recall")
    assert "/old/" not in by_matcher["Bash|Edit|Write|NotebookEdit"]


def test_init_repoints_the_mcp_entry_with_the_hooks(fakehome: Path):
    mcp = str(Path(sys.executable).parent / "skillmem-mcp")
    claude_json = fakehome / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {
        "skillmem": {"command": "/old/venv/bin/skillmem-mcp", "args": [], "env": {"X": "1"}},
        "other": {"command": "/old/venv/bin/skillmem-mcp"}}}))
    _run(["init", "--claude-code", "--mcp-binary", mcp, "--skip-migrate"])
    servers = json.loads(claude_json.read_text())["mcpServers"]
    assert servers["skillmem"]["command"] == mcp
    assert servers["skillmem"]["env"] == {"X": "1"}
    assert servers["other"]["command"] == "/old/venv/bin/skillmem-mcp"   # not ours


def test_settings_backups_do_not_overwrite_each_other_within_a_second(fakehome: Path, monkeypatch):
    import time
    from skillmem import cli as _cli
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.4)   # helpers do `import time as _time`
    settings_json = fakehome / ".claude" / "settings.json"
    settings_json.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"permissions": {"deny": []}, "hooks": {}})
    settings_json.write_text(original)
    _cli._patch_settings_deny(settings_json, "Bash(skillmem trust*)")
    _cli._patch_settings_hook(settings_json, Path("/v/bin/skillmem"), event="Stop", args=["hook", "a"])
    _cli._patch_settings_hook(settings_json, Path("/v/bin/skillmem"), event="Stop", args=["hook", "b"])
    backups = sorted(settings_json.parent.glob("settings.json.bak.*"))
    assert len(backups) == 3
    assert backups[0].read_text() == original
