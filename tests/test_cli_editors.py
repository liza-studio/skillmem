"""`skillmem init` wires Cursor, Windsurf, Gemini CLI and opencode, and undoes it.

Four editors, two config shapes: the Claude-style ``mcpServers`` map (Cursor,
Windsurf, Gemini CLI) and opencode's own ``mcp`` block with an argv command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from skillmem.cli import main as cli_main

MCP_BIN = str(Path(sys.executable).parent / "skillmem-mcp")

#: agent flag → config path relative to $HOME
EDITORS = {
    "cursor": Path(".cursor") / "mcp.json",
    "windsurf": Path(".codeium") / "windsurf" / "mcp_config.json",
    "gemini": Path(".gemini") / "settings.json",
}
OPENCODE = Path(".config") / "opencode" / "opencode.json"


def _run(args: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli_main, args, catch_exceptions=False)
    return result.exit_code, result.output


def _init(*extra: str) -> tuple[int, str]:
    return _run(["init", "--mcp-binary", MCP_BIN, "--skip-migrate", *extra])


@pytest.mark.parametrize("agent", sorted(EDITORS))
def test_init_editor_creates_mcp_entry(fakehome: Path, agent: str):
    """A machine with no config yet still gets a valid one."""
    assert _init(f"--{agent}")[0] == 0
    cfg = json.loads((fakehome / EDITORS[agent]).read_text(encoding="utf-8"))
    entry = cfg["mcpServers"]["skillmem"]
    assert entry["command"] == MCP_BIN
    assert entry["args"] == []
    # Authorship: skills this editor writes stay distinguishable in a shared DB.
    assert entry["env"]["SKILLMEM_AGENT"] == agent


@pytest.mark.parametrize("agent", sorted(EDITORS))
def test_init_editor_preserves_existing_servers(fakehome: Path, agent: str):
    """The user's own MCP servers and unrelated settings survive."""
    path = fakehome / EDITORS[agent]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mcpServers": {"github": {"command": "gh-mcp", "args": []}},
        "theme": "dark",
    }), encoding="utf-8")

    assert _init(f"--{agent}")[0] == 0
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["github"]["command"] == "gh-mcp"
    assert cfg["theme"] == "dark"
    assert "skillmem" in cfg["mcpServers"]


@pytest.mark.parametrize("agent", sorted(EDITORS))
def test_init_editor_is_idempotent(fakehome: Path, agent: str):
    """Running init twice reports no change rather than duplicating the entry."""
    assert _init(f"--{agent}")[0] == 0
    code, out = _init(f"--{agent}")
    assert code == 0
    assert "already configured" in out


def test_init_editor_refuses_invalid_json(fakehome: Path):
    """A corrupt config is backed up and left alone, never overwritten."""
    path = fakehome / EDITORS["cursor"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert _init("--cursor")[0] == 0
    assert path.read_text(encoding="utf-8") == "{ not json"
    assert list(path.parent.glob("mcp.json.bak.*"))


def test_init_opencode_uses_its_own_shape(fakehome: Path):
    """opencode keeps servers under `mcp` with an argv command and `environment`."""
    assert _init("--opencode")[0] == 0
    cfg = json.loads((fakehome / OPENCODE).read_text(encoding="utf-8"))
    entry = cfg["mcp"]["skillmem"]
    assert entry["type"] == "local"
    assert entry["command"] == [MCP_BIN]
    assert entry["enabled"] is True
    assert entry["environment"]["SKILLMEM_AGENT"] == "opencode"


def test_init_all_agents_wires_every_editor(fakehome: Path):
    """--all-agents is the six-agent shortcut, not a subset of them."""
    assert _init("--all-agents")[0] == 0
    for rel in EDITORS.values():
        assert (fakehome / rel).exists(), rel
    assert (fakehome / OPENCODE).exists()
    assert (fakehome / ".codex" / "config.toml").exists()
    assert (fakehome / ".claude.json").exists()


def test_uninstall_removes_editor_entries(fakehome: Path):
    """Undo is symmetric: our entry goes, the user's stays."""
    path = fakehome / EDITORS["cursor"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "mcpServers": {"github": {"command": "gh-mcp", "args": []}},
    }), encoding="utf-8")

    assert _init("--cursor", "--opencode")[0] == 0
    assert _run(["uninstall"])[0] == 0

    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert "skillmem" not in cfg["mcpServers"]
    assert cfg["mcpServers"]["github"]["command"] == "gh-mcp"

    oc = json.loads((fakehome / OPENCODE).read_text(encoding="utf-8"))
    assert "skillmem" not in (oc.get("mcp") or {})
