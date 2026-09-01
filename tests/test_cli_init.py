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
    # full mode: Stop = migrate + session-recap, exactly two
    hook_count = sum(len(grp["hooks"]) for grp in settings2["hooks"]["Stop"])
    assert hook_count == 2


def test_init_hooks_minimal(fakehome: Path):
    """--hooks minimal keeps the <=0.7 behaviour: only Stop→migrate."""
    _run(["init", "--claude-code", "--hooks", "minimal", "--mcp-binary",
          str(Path(sys.executable).parent / "skillmem-mcp"), "--skip-migrate"])
    settings = json.loads((fakehome / ".claude" / "settings.json").read_text())
    assert list(settings["hooks"].keys()) == ["Stop"]
    assert sum(len(g["hooks"]) for g in settings["hooks"]["Stop"]) == 1


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
