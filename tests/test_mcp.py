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


def test_reinforce_refuses_a_record_that_is_not_a_skill(mcp):
    _payload(mcp._tool_write({"slug": "plain-note", "title": "note", "body": "just a note"}))
    err = _payload(mcp._tool_reinforce({"slug": "plain-note", "evidence": "test_passed"}))
    assert "not a skill" in err.get("error", "")
    from skillmem import storage as S
    assert S.get(mcp._shared_conn(), "plain-note").strength == 1.0


def test_recall_limit_is_capped_at_fifty(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    for i in range(70):
        S.upsert(conn, S.MemoryItem(slug=f"skill-c-{i}", kind="skill", title=f"common skill {i}",
                                    body="common words for every skill " * 3), check_conflicts=False)
    assert len(_payload(mcp._tool_recall({"query": "common words", "limit": 100,
                                          "auto_reinforce": False}))["skills"]) <= 50


def test_learn_same_text_returns_the_existing_skill_and_conflicts_name_notes(mcp):
    body = "the exact same steps written once and repeated verbatim here"
    a = _payload(mcp._tool_learn({"slug": "skill-same", "title": "same", "trigger": body,
                                  "steps": body, "outcome": "success", "lessons": "none"}))
    b = _payload(mcp._tool_learn({"slug": "skill-same", "title": "same", "trigger": body,
                                  "steps": body, "outcome": "success", "lessons": "none"}))
    assert a["ok"] and b["ok"]                       # byte-identical is not a conflict
    _payload(mcp._tool_write({"slug": "a-note", "title": "note",
                              "body": "deployment rollback checklist for the release train"}))
    err = _payload(mcp._tool_learn({"slug": "skill-dup", "title": "dup",
                                    "trigger": "deployment rollback checklist for the release train",
                                    "steps": "deployment rollback checklist for the release train",
                                    "outcome": "success", "lessons": "none"}))
    assert "a-note" in err.get("error", "")          # a plain note is named, as the description says


def test_archive_keeps_updated_at_and_restore_survives_the_sweep(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-idle", "title": "idle", "trigger": "rarely used path",
                              "steps": "step", "outcome": "success", "lessons": "none"}))
    old = S._now() - 100 * 86400
    conn.execute("UPDATE memory_items SET updated_at = ?, last_accessed_at = ?, strength = 0.05 "
                 "WHERE slug = 'skill-idle'", (old, old))
    _payload(mcp._tool_archive({"slug": "skill-idle"}))
    assert S.get(conn, "skill-idle").updated_at == old      # archiving is not an edit
    _payload(mcp._tool_archive({"slug": "skill-idle", "archived": False}))
    S.sweep_lifecycle(conn)
    row = conn.execute("SELECT lifecycle FROM memory_items WHERE slug = 'skill-idle'").fetchone()
    assert row["lifecycle"] == "active"                     # the sweep does not undo a restore


def test_pin_and_archive_do_not_touch_updated_at_or_hand_out_strength(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-flags", "title": "flags", "trigger": "rare path",
                              "steps": "step", "outcome": "success", "lessons": "none"}))
    old = S._now() - 50 * 86400
    conn.execute("UPDATE memory_items SET updated_at = ?, strength = 0.05 WHERE slug = 'skill-flags'", (old,))
    _payload(mcp._tool_pin({"slug": "skill-flags"}))
    assert S.get(conn, "skill-flags").updated_at == old          # pinning is not an edit
    _payload(mcp._tool_pin({"slug": "skill-flags", "pinned": False}))
    r = _payload(mcp._tool_archive({"slug": "skill-flags", "archived": False}))
    assert r["was"] == "active" and r["lifecycle"] == "active"
    assert S.get(conn, "skill-flags").strength == 0.05           # no strength without evidence


def test_learn_refuses_a_slug_that_already_holds_a_note(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    body = S.skill_body("a condition worth recording", "an action taken", "success", None)
    S.upsert(conn, S.MemoryItem(slug="looks-like-skill", kind="note", title="taken",
                                body=body), check_conflicts=False)
    err = _payload(mcp._tool_learn({"slug": "looks-like-skill", "title": "taken",
                                    "trigger": "a condition worth recording",
                                    "steps": "an action taken", "outcome": "success",
                                    "lessons": None, "check_conflicts": False}))
    assert "already holds a note" in err.get("error", "")
    assert S.get(conn, "looks-like-skill").kind == "note"


def test_agent_cannot_archive_what_the_owner_approved(mcp):
    """Archiving hides a record from every read without touching text or approval,
    so an agent doing it to the owner's own rule leaves nothing a later read shows."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="rule-gate", kind="feedback", title="deploy gate",
                                body="deploy only through the gate, never by hand",
                                origin="owner"))
    conn.execute("UPDATE memory_items SET trusted_at = ?, trusted_by = 'owner' "
                 "WHERE slug = 'rule-gate'", (1_700_000_000,))
    err = _payload(mcp._tool_archive({"slug": "rule-gate"}))
    assert "cannot archive" in err.get("error", ""), err
    assert "skills-archive" in err["error"]
    row = conn.execute("SELECT lifecycle FROM memory_items WHERE slug='rule-gate'").fetchone()
    assert row["lifecycle"] == "active"
    # the owner's own path still works
    assert S.set_archived(conn, "rule-gate", True, by="owner-cli")["lifecycle"] == "archived"


def test_archiving_leaves_a_history_row(mcp):
    """The owner's only trace of a record leaving every read."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-temp", "title": "temp", "trigger": "a trigger here",
                              "steps": "the steps taken", "outcome": "success", "lessons": None}))
    before = conn.execute("SELECT COUNT(*) c FROM memory_history WHERE slug='skill-temp'").fetchone()["c"]
    _payload(mcp._tool_archive({"slug": "skill-temp"}))
    _payload(mcp._tool_archive({"slug": "skill-temp", "archived": False}))
    rows = conn.execute("SELECT reason FROM memory_history WHERE slug='skill-temp' "
                        "ORDER BY id").fetchall()
    assert len(rows) == before + 2, [r["reason"] for r in rows]
    assert rows[-2]["reason"] == "archived"
    assert rows[-1]["reason"].startswith("restored from")
    _, broken = S.verify_history(conn)
    assert broken == []                           # the hash chain still verifies


def test_pinning_writes_the_flag_and_nothing_else(mcp):
    """mem_pin's description says the flag only; no lifecycle, no free strength."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-gate", "title": "gate", "trigger": "deploy gate rule",
                              "steps": "always through the gate", "outcome": "success", "lessons": "none"}))
    conn.execute("UPDATE memory_items SET strength = 0.05 WHERE slug = 'skill-gate'")
    _payload(mcp._tool_archive({"slug": "skill-gate"}))
    before = conn.execute("SELECT lifecycle, strength, last_accessed_at, updated_at "
                          "FROM memory_items WHERE slug='skill-gate'").fetchone()
    _payload(mcp._tool_pin({"slug": "skill-gate"}))
    after = conn.execute("SELECT lifecycle, strength, last_accessed_at, updated_at, pinned "
                         "FROM memory_items WHERE slug='skill-gate'").fetchone()
    assert after["pinned"] == 1
    assert (after["lifecycle"], after["strength"]) == (before["lifecycle"], before["strength"])
    assert after["last_accessed_at"] == before["last_accessed_at"]
    assert after["updated_at"] == before["updated_at"]
    # the explicit verb is the way back, and it works on a pinned row
    r = _payload(mcp._tool_archive({"slug": "skill-gate", "archived": False}))
    assert r["lifecycle"] == "active"
    assert any(i["slug"] == "skill-gate" for i in _payload(mcp._tool_list({"limit": 50}))["items"])


def test_recall_limit_argument_is_clamped_to_fifty(mcp, monkeypatch):
    """The clamp has to be applied by mem_recall itself, not merely available."""
    from skillmem import storage as S
    seen = {}
    real = S.recall_skills

    def spy(conn, query, **kw):
        seen["limit"] = kw.get("limit")
        return real(conn, query, **kw)

    monkeypatch.setattr(S, "recall_skills", spy)
    _payload(mcp._tool_recall({"query": "deploy gate", "limit": 1000}))
    assert seen["limit"] == 50                  # what the description promises
    assert mcp._limit({"limit": 100}, 5) == 100  # other tools keep the 100 cap


def test_learn_refusal_leaves_the_existing_record_alone(mcp):
    """The kind check used to run after the upsert: a refused call still landed its tags."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="looks-like-skill", kind="note", title="a note",
                                body="owner's own note about the deploy gate", tags=["before"]))
    before = S.get(conn, "looks-like-skill")
    err = _payload(mcp._tool_learn({"slug": "looks-like-skill", "title": "a note",
                                    "trigger": "deploy gate rule", "steps": "an action taken",
                                    "outcome": "success", "lessons": None,
                                    "tags": ["after"], "check_conflicts": False}))
    assert "already holds a note" in err.get("error", "")
    after = S.get(conn, "looks-like-skill")
    assert (after.kind, after.tags, after.body) == (before.kind, before.tags, before.body)
    assert after.updated_at == before.updated_at


def test_learn_conflict_names_no_parameter_this_tool_lacks(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="skill-dup", kind="skill", title="dup",
                                body="the original body of this skill record"))
    err = _payload(mcp._tool_learn({"slug": "skill-dup", "title": "dup",
                                    "trigger": "a different trigger entirely",
                                    "steps": "different steps", "outcome": "success",
                                    "lessons": None}))
    msg = err.get("error", "")
    assert "force=" not in msg and "reason=" not in msg, msg


def test_write_refusal_names_only_parameters_this_tool_has(mcp):
    _payload(mcp._tool_write({"slug": "w1", "title": "one", "body": "first body text here"}))
    err = _payload(mcp._tool_write({"slug": "w1", "title": "one", "body": "different body text"}))
    msg = err.get("error", "")
    # mem_write and mem_learn carry neither reason nor force; the message must
    # not send the agent after a parameter its schema does not have
    assert "reason" not in msg and "force" not in msg, msg
    assert "update" in msg.lower()
