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


def test_tools_and_handlers_match(mcp):
    names = [t.name for t in mcp.TOOLS]
    assert len(names) == 10
    assert len(set(names)) == 10
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

    # Self-report is recorded, not rewarded.
    noted = _payload(mcp.TOOL_HANDLERS["mem_reinforce"]({"slug": "skill-nginx-reload"}))
    assert noted["strength"] == 1.0 and noted["access_count"] == 1

    # An outside signal is what actually moves it.
    boosted = _payload(mcp.TOOL_HANDLERS["mem_reinforce"](
        {"slug": "skill-nginx-reload", "evidence": "test_passed"}))
    assert boosted["strength"] > 1.0 and boosted["confirmed_count"] == 1

    bad = _payload(mcp.TOOL_HANDLERS["mem_reinforce"](
        {"slug": "skill-nginx-reload", "evidence": "looks_fine"}))
    assert "error" in bad

    pinned = _payload(mcp.TOOL_HANDLERS["mem_pin"]({"slug": "skill-nginx-reload"}))
    assert pinned["pinned"] is True

    missing = _payload(mcp.TOOL_HANDLERS["mem_reinforce"]({"slug": "no-such-skill"}))
    assert "error" in missing


# --------------------------------------------------------------------------- #
# the tool descriptions are a contract: an agent acts on them without reading
# the code, so what they promise has to be true
# --------------------------------------------------------------------------- #


def _desc(mcp, name: str) -> str:
    return next(t.description for t in mcp.TOOLS if t.name == name)


def test_descriptions_do_not_promise_behaviour_we_lack(mcp):
    """Caught by review: mem_search claimed it excluded session recaps, which only
    the CLI does. A description that lies is worse than a thin one — the agent
    cannot check it."""
    search = _desc(mcp, "mem_search")
    assert "excluded by default" not in search, "mem_search does not filter kinds"

    # what the read-only tools claim
    for name in ("mem_search", "mem_get", "mem_list"):
        assert "ead-only" in _desc(mcp, name), f"{name} should say it is read-only"

    # and what the writers must disclose
    for name in ("mem_write", "mem_learn", "mem_update"):
        d = _desc(mcp, name)
        assert "WRITES" in d, f"{name} should disclose that it writes"
    assert "UNAPPROVED" in _desc(mcp, "mem_learn")
    assert "DROPS" in _desc(mcp, "mem_update")
    assert "SIDE EFFECT" in _desc(mcp, "mem_recall")


def test_read_only_tools_really_are(mcp, conn):
    """mem_get and mem_list say 'no side effects' — hold them to it."""
    from skillmem import storage as S
    S.upsert(conn, S.MemoryItem(slug="skill-ro", kind="skill", origin="agent",
                                title="Прогон тестов перед деплоем",
                                body="trigger: деплой; steps: pytest."))
    conn.commit()
    before = S.get(conn, "skill-ro")
    _payload(mcp.TOOL_HANDLERS["mem_get"]({"slug": "skill-ro"}))
    _payload(mcp.TOOL_HANDLERS["mem_list"]({}))
    after = S.get(conn, "skill-ro")
    assert (after.access_count, after.updated_at, after.strength) == \
           (before.access_count, before.updated_at, before.strength)


def test_recall_refreshes_recency_but_never_strength(mcp, conn):
    """mem_recall's description draws exactly this line; if the code ever stops
    honouring it, an agent could promote its own guesses by re-reading them."""
    from skillmem import storage as S
    S.upsert(conn, S.MemoryItem(slug="skill-recency", kind="skill", origin="agent",
                                title="Дренаж перед рестартом сервиса",
                                body="trigger: рестарт; steps: сначала дренаж."))
    conn.commit()
    before = S.get(conn, "skill-recency").strength
    _payload(mcp.TOOL_HANDLERS["mem_recall"]({"query": "рестарт сервиса",
                                              "auto_reinforce": True}))
    after = S.get(conn, "skill-recency")
    assert after.strength == before, "auto_reinforce must not raise strength"
    assert after.access_count > 0, "but it should mark the skill as retrieved"


def test_descriptions_do_not_overclaim_cross_language(mcp):
    """Snowball stems within a language; it does not translate. Claiming that a
    Russian query finds an English record without the semantic extra sold
    behaviour the default install does not have."""
    d = _desc(mcp, "mem_search")
    assert "semantic" in d, "cross-language must be attributed to the semantic layer"
    assert "so a query in one language finds a record written in the other" not in d


def test_identical_rewrite_is_described_honestly(mcp):
    """mem_write returns the existing record when the text is byte-identical
    instead of raising — the description has to say so."""
    assert "byte-identical" in _desc(mcp, "mem_write")


# --------------------------------------------------------------------------- #
# archive (0.11.1)
# --------------------------------------------------------------------------- #


def test_archive_hides_from_search_recall_list_but_get_still_reads(mcp):
    _payload(mcp._tool_learn({"slug": "skill-retire-me", "title": "retire me",
                              "trigger": "quartz calibration bench", "steps": "step one step two",
                              "outcome": "success", "lessons": "none"}))
    r = _payload(mcp._tool_archive({"slug": "skill-retire-me"}))
    assert r["lifecycle"] == "archived" and r["was"] == "active"
    assert all(h["slug"] != "skill-retire-me" for h in _payload(mcp._tool_search({"query": "quartz calibration"}))["results"])
    assert all(h["slug"] != "skill-retire-me" for h in _payload(mcp._tool_recall({"query": "quartz calibration", "auto_reinforce": False}))["skills"])
    assert all(i["slug"] != "skill-retire-me" for i in _payload(mcp._tool_list({"limit": 50}))["items"])
    got = _payload(mcp._tool_get({"slug": "skill-retire-me"}))
    assert got["slug"] == "skill-retire-me"
    back = _payload(mcp._tool_archive({"slug": "skill-retire-me", "archived": False}))
    assert back["lifecycle"] == "active" and back["was"] == "archived"
    assert any(i["slug"] == "skill-retire-me" for i in _payload(mcp._tool_list({"limit": 50}))["items"])


def test_archive_refuses_pinned_and_unknown(mcp):
    _payload(mcp._tool_write({"slug": "gate-rule", "title": "deploy gate", "body": "only through the gate"}))
    _payload(mcp._tool_pin({"slug": "gate-rule"}))
    err = _payload(mcp._tool_archive({"slug": "gate-rule"}))
    assert "pinned" in err.get("error", "")
    err = _payload(mcp._tool_archive({"slug": "no-such-slug"}))
    assert "not found" in err.get("error", "")
