"""`skillmem init --codex` writes a valid Codex MCP entry, and uninstall removes it."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from click.testing import CliRunner

from skillmem.cli import main as cli_main

MCP_BIN = str(Path(sys.executable).parent / "skillmem-mcp")


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli_main, args, catch_exceptions=False)
    return result.exit_code, result.output


def _init(fakehome: Path, *extra: str) -> tuple[int, str]:
    return _run(["init", "--codex", "--mcp-binary", MCP_BIN,
                 "--skip-migrate", *extra])


def _config(fakehome: Path) -> Path:
    return fakehome / ".codex" / "config.toml"


def test_init_codex_creates_mcp_table(fakehome: Path):
    """A fresh machine with no ~/.codex yet still gets a valid config."""
    code, _ = _init(fakehome)
    assert code == 0
    cfg = tomllib.loads(_config(fakehome).read_text(encoding="utf-8"))
    entry = cfg["mcp_servers"]["skillmem"]
    assert entry["command"] == MCP_BIN
    assert entry["args"] == []
    # Authorship: skills Codex writes must be distinguishable from Claude's.
    assert entry["env"]["SKILLMEM_AGENT"] == "codex"


def test_init_codex_preserves_existing_config(fakehome: Path):
    """Appending must not disturb the user's own settings or comments."""
    cfg_path = _config(fakehome)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        '# my own notes\n'
        'model = "gpt-5.4-codex"\n'
        '\n'
        '[projects."/srv/app"]\n'
        'trust_level = "trusted"\n'
    )
    cfg_path.write_text(original, encoding="utf-8")

    assert _init(fakehome)[0] == 0
    raw = cfg_path.read_text(encoding="utf-8")
    assert raw.startswith(original)          # untouched, verbatim
    assert "# my own notes" in raw
    cfg = tomllib.loads(raw)
    assert cfg["model"] == "gpt-5.4-codex"
    assert cfg["projects"]["/srv/app"]["trust_level"] == "trusted"
    assert "skillmem" in cfg["mcp_servers"]


def test_init_codex_keeps_sibling_mcp_servers(fakehome: Path):
    """A user who already runs another MCP server keeps it."""
    cfg_path = _config(fakehome)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        '[mcp_servers.other]\ncommand = "/usr/bin/other-mcp"\n', encoding="utf-8")

    assert _init(fakehome)[0] == 0
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    assert set(cfg["mcp_servers"]) == {"other", "skillmem"}


def test_init_codex_idempotent(fakehome: Path):
    """Second run must not append a duplicate table (TOML would reject it)."""
    _init(fakehome)
    first = _config(fakehome).read_text(encoding="utf-8")
    code, out = _init(fakehome)
    assert code == 0
    assert "already configured" in out
    assert _config(fakehome).read_text(encoding="utf-8") == first


def test_init_codex_refuses_broken_toml(fakehome: Path):
    """Invalid TOML is left alone with a backup — never silently overwritten."""
    cfg_path = _config(fakehome)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    broken = 'model = "unterminated\n'
    cfg_path.write_text(broken, encoding="utf-8")

    code, out = _init(fakehome)
    assert code == 0
    assert cfg_path.read_text(encoding="utf-8") == broken
    assert "invalid TOML" in out
    assert list(cfg_path.parent.glob("config.toml.bak.*"))


def test_uninstall_codex_removes_only_skillmem(fakehome: Path):
    """Round-trip: the user's config comes back as it was."""
    cfg_path = _config(fakehome)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        '# keep me\n'
        'model = "gpt-5.4-codex"\n'
        '\n'
        '[mcp_servers.other]\n'
        'command = "/usr/bin/other-mcp"\n'
    )
    cfg_path.write_text(original, encoding="utf-8")
    _init(fakehome)

    code, _ = _run(["uninstall", "--keep-db"])
    assert code == 0
    raw = cfg_path.read_text(encoding="utf-8")
    cfg = tomllib.loads(raw)
    assert "skillmem" not in cfg["mcp_servers"]
    assert cfg["mcp_servers"]["other"]["command"] == "/usr/bin/other-mcp"
    assert "# keep me" in raw


def test_uninstall_codex_noop_when_absent(fakehome: Path):
    """Uninstall on a machine that never ran init --codex must not crash."""
    code, _ = _run(["uninstall", "--keep-db"])
    assert code == 0
