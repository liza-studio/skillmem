"""MCP server contract tests — handlers called directly, no stdio transport.

The stdio wiring is a thin adapter over TOOLS/TOOL_HANDLERS; everything
observable (tool roster, payload shapes, server-side authorship) is testable
by invoking the handler functions with plain dicts.
"""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

from skillmem import storage as S

README = Path(__file__).resolve().parents[1] / "README.md"


@pytest.fixture
def mcp(memhome):
    """mcp_server module with a fresh cached connection under tmp HOME."""
    from skillmem import mcp_server as m
    m._CONN = None
    yield m
    if m._CONN is not None:
        m._CONN.close()
        m._CONN = None


def _payload(result) -> dict:
    assert len(result) == 1 and result[0].type == "text"
    return json.loads(result[0].text)


# --------------------------------------------------------------------------- #
# tool roster
# --------------------------------------------------------------------------- #


def test_eight_tools_and_handlers_match(mcp):
    names = [t.name for t in mcp.TOOLS]
    assert len(names) == 8
    assert len(set(names)) == 8
    assert set(names) == set(mcp.TOOL_HANDLERS)


def test_tool_roster_matches_readme(mcp):
    text = README.read_text(encoding="utf-8")
    section = text.split("## MCP tools", 1)[1].split("\n## ", 1)[0]
    readme_tools = set(re.findall(r"`(mem_[a-z_]+)`", section))
    assert readme_tools == {t.name for t in mcp.TOOLS}


# --------------------------------------------------------------------------- #
# write -> search -> get
# --------------------------------------------------------------------------- #


def test_write_then_search_finds_record(mcp):
    res = _payload(mcp.TOOL_HANDLERS["mem_write"]({
        "slug": "note-wal-checkpoint",
        "title": "SQLite WAL checkpoint tuning",
        "body": "Run PRAGMA wal_checkpoint(TRUNCATE) after bulk imports.",
        "kind": "note",
        "tags": ["sqlite"],
    }))
    assert res.get("ok") is True and res["slug"] == "note-wal-checkpoint"

    found = _payload(mcp.TOOL_HANDLERS["mem_search"]({"query": "wal checkpoint"}))
    assert found["count"] >= 1
    assert any(r["slug"] == "note-wal-checkpoint" for r in found["results"])

    got = _payload(mcp.TOOL_HANDLERS["mem_get"]({"slug": "note-wal-checkpoint"}))
    assert "wal_checkpoint" in got["body"]


def test_write_refuses_duplicate_slug(mcp):
    args = {"slug": "dup-slug", "title": "first", "body": "unique body one two three"}
    assert _payload(mcp.TOOL_HANDLERS["mem_write"](args)).get("ok") is True
    res = _payload(mcp.TOOL_HANDLERS["mem_write"](
        {"slug": "dup-slug", "title": "second", "body": "completely different text here"}
    ))
    assert "error" in res and "dup-slug" in res["error"]


def test_update_requires_reason(mcp):
    mcp.TOOL_HANDLERS["mem_write"](
        {"slug": "upd-me", "title": "t", "body": "original body of the note"}
    )
    res = _payload(mcp.TOOL_HANDLERS["mem_update"]({"slug": "upd-me", "body": "new"}))
    assert "error" in res
    res = _payload(mcp.TOOL_HANDLERS["mem_update"](
        {"slug": "upd-me", "body": "new body text", "reason": "test edit"}
    ))
    assert res.get("ok") is True and res["history_entries"] == 1


# --------------------------------------------------------------------------- #
# authorship is server-side (SKILLMEM_AGENT), never client-supplied
# --------------------------------------------------------------------------- #


def test_client_agent_argument_cannot_forge_authorship(mcp):
    mcp.TOOL_HANDLERS["mem_write"]({
        "slug": "authored", "title": "who wrote this",
        "body": "body text for authorship check",
        "agent": "intruder",  # client-supplied — must be ignored
    })
    item = S.get(mcp._shared_conn(), "authored")
    assert item.agent == mcp._agent()
    assert item.agent != "intruder"


def test_agent_identity_comes_from_env(memhome, monkeypatch):
    """SKILLMEM_AGENT is read at import time — reload with the env set."""
    from skillmem import mcp_server
    monkeypatch.setenv("SKILLMEM_AGENT", "engineer-bot")
    mod = importlib.reload(mcp_server)
    mod._CONN = None
    try:
        assert mod._agent() == "engineer-bot"
        mod.TOOL_HANDLERS["mem_write"]({
            "slug": "env-authored", "title": "env identity",
            "body": "written under an env-provided identity",
            "agent": "spoof-attempt",
        })
        assert S.get(mod._shared_conn(), "env-authored").agent == "engineer-bot"
    finally:
        if mod._CONN is not None:
            mod._CONN.close()
        monkeypatch.delenv("SKILLMEM_AGENT")
        restored = importlib.reload(mcp_server)
        restored._CONN = None


def test_agent_falls_back_to_client_name(mcp, monkeypatch):
    """With no SKILLMEM_AGENT set, the MCP client's own name is the author.

    That is what makes a plugin work in any agent without configuration:
    Codex is recorded as codex, not as claude-code.
    """
    monkeypatch.setattr(mcp, "_ENV_AGENT", None)
    monkeypatch.setattr(mcp, "_client_agent", "codex")
    mcp.TOOL_HANDLERS["mem_write"]({
        "slug": "client-authored", "title": "client identity",
        "body": "written by whichever agent connected",
    })
    assert S.get(mcp._shared_conn(), "client-authored").agent == "codex"


def test_env_agent_beats_client_name(mcp, monkeypatch):
    """An explicit SKILLMEM_AGENT always wins over what the client claims."""
    monkeypatch.setattr(mcp, "_ENV_AGENT", "engineer-bot")
    monkeypatch.setattr(mcp, "_client_agent", "codex")
    mcp.TOOL_HANDLERS["mem_write"]({
        "slug": "env-wins", "title": "env priority",
        "body": "env identity must outrank the handshake",
    })
    assert S.get(mcp._shared_conn(), "env-wins").agent == "engineer-bot"


def test_agent_defaults_when_client_is_silent(mcp, monkeypatch):
    """No env, no clientInfo — the historical default keeps databases uniform."""
    monkeypatch.setattr(mcp, "_ENV_AGENT", None)
    monkeypatch.setattr(mcp, "_client_agent", None)
    mcp.TOOL_HANDLERS["mem_write"]({
        "slug": "silent-client", "title": "no identity offered",
        "body": "falls back to the pre-clientInfo default",
    })
    assert S.get(mcp._shared_conn(), "silent-client").agent == "claude-code"


def test_normalize_agent_slugifies_free_form_names(mcp):
    """clientInfo.name is arbitrary text from another vendor — never trust its shape."""
    assert mcp._normalize_agent("Codex CLI") == "codex-cli"
    assert mcp._normalize_agent("  Claude Code  ") == "claude-code"
    assert mcp._normalize_agent("weird!!name///") == "weird-name"
    assert mcp._normalize_agent("!!!") == "unknown"
    assert len(mcp._normalize_agent("x" * 200)) == 40


# --------------------------------------------------------------------------- #
# learn -> recall -> reinforce
# --------------------------------------------------------------------------- #


def test_learn_recall_reinforce_cycle(mcp):
    res = _payload(mcp.TOOL_HANDLERS["mem_learn"]({
        "slug": "skill-nginx-reload",
        "title": "Reload nginx without downtime",
        "trigger": "config change on the load balancer",
        "steps": "nginx -t, then systemctl reload nginx",
        "outcome": "success",
        "lessons": "always test config before reload",
    }))
    assert res.get("ok") is True and res["kind"] == "skill"

    recalled = _payload(mcp.TOOL_HANDLERS["mem_recall"](
        {"query": "reload nginx config", "auto_reinforce": False}
    ))
    assert any(s["slug"] == "skill-nginx-reload" for s in recalled["skills"])

    boosted = _payload(mcp.TOOL_HANDLERS["mem_reinforce"]({"slug": "skill-nginx-reload"}))
    assert boosted["strength"] > 1.0 and boosted["access_count"] == 1

    missing = _payload(mcp.TOOL_HANDLERS["mem_reinforce"]({"slug": "no-such-skill"}))
    assert "error" in missing
