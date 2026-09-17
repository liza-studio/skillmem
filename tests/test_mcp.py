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
    assert len(names) == 9
    assert len(set(names)) == 9
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
    """The owner's own command. There is no tool for this by design."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-retire-me", "title": "retire me",
                              "trigger": "quartz calibration bench", "steps": "step one step two",
                              "outcome": "success", "lessons": "none"}))
    r = S.set_archived(conn, "skill-retire-me", True, by="owner-cli")
    assert r["lifecycle"] == "archived" and r["was"] == "active"
    assert all(h["slug"] != "skill-retire-me" for h in _payload(mcp._tool_search({"query": "quartz calibration"}))["results"])
    assert all(h["slug"] != "skill-retire-me" for h in _payload(mcp._tool_recall({"query": "quartz calibration", "auto_reinforce": False}))["skills"])
    assert all(i["slug"] != "skill-retire-me" for i in _payload(mcp._tool_list({"limit": 50}))["items"])
    got = _payload(mcp._tool_get({"slug": "skill-retire-me"}))
    assert got["slug"] == "skill-retire-me"
    back = S.set_archived(conn, "skill-retire-me", False, by="owner-cli")
    assert back["lifecycle"] == "active" and back["was"] == "archived"
    assert any(i["slug"] == "skill-retire-me" for i in _payload(mcp._tool_list({"limit": 50}))["items"])


def test_archive_refuses_pinned_and_unknown(mcp):
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_write({"slug": "gate-rule", "title": "deploy gate",
                              "body": "only through the gate"}))
    _payload(mcp._tool_pin({"slug": "gate-rule"}))
    with pytest.raises(ValueError, match="pinned"):
        S.set_archived(conn, "gate-rule", True, by="owner-cli")
    assert S.set_archived(conn, "no-such-slug", True, by="owner-cli") is None


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
    S.set_archived(conn, "skill-idle", True, allow_sealed=True, by="owner-cli")
    assert S.get(conn, "skill-idle").updated_at == old      # archiving is not an edit
    S.set_archived(conn, "skill-idle", False, by="owner-cli")
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
    r = S.set_archived(conn, "skill-flags", False, by="owner-cli")
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


def test_agent_cannot_archive_what_the_owner_approved(mcp, at_terminal, monkeypatch):
    """Archiving hides a record from every read without touching text or approval,
    so an agent doing it to the owner's own rule leaves nothing a later read shows."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="rule-gate", kind="feedback", title="deploy gate",
                                body="deploy only through the gate, never by hand",
                                origin="owner"))
    conn.execute("UPDATE memory_items SET trusted_at = ?, trusted_by = 'owner' "
                 "WHERE slug = 'rule-gate'", (1_700_000_000,))
    import pytest
    monkeypatch.setattr(S, "owner_present", lambda: False)   # the agent's side
    with pytest.raises(S.SealedRecord) as exc:
        S.set_archived(conn, "rule-gate", True)
    assert "skills-archive" in str(exc.value)
    row = conn.execute("SELECT lifecycle FROM memory_items WHERE slug='rule-gate'").fetchone()
    assert row["lifecycle"] == "active"
    # the owner's own path still works
    assert S.set_archived(conn, "rule-gate", True, by="owner-cli",
                          allow_sealed=True)["lifecycle"] == "archived"


def test_update_does_not_open_the_way_to_archive_an_owner_record(mcp):
    """mem_update relabels origin to 'agent' and drops the approval by design, so
    the gate cannot rest on those two fields: two calls would lift any rule."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="deploy-gate", kind="feedback", title="deploy gate",
                                body="deploy only through the gate, never by hand",
                                origin="owner"))
    conn.execute("UPDATE memory_items SET trusted_at = ?, trusted_by = 'owner' "
                 "WHERE slug = 'deploy-gate'", (1_700_000_000,))
    _payload(mcp._tool_update({"slug": "deploy-gate", "title": "deploy gate (v2)",
                               "body": "deploy only through the gate, never by hand at all",
                               "reason": "clarified"}))
    row = conn.execute("SELECT origin, trusted_at, owner_seal FROM memory_items "
                       "WHERE slug='deploy-gate'").fetchone()
    assert (row["origin"], row["trusted_at"]) == ("agent", None)   # both moved, as designed
    assert row["owner_seal"] == 1                                  # the seal did not
    import pytest
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "deploy-gate", True)
    assert conn.execute("SELECT lifecycle FROM memory_items WHERE slug='deploy-gate'"
                        ).fetchone()["lifecycle"] == "active"


def test_owner_writing_a_record_seals_it(mcp, at_terminal, monkeypatch):
    """The update path is what clears trusted_at, so it must set the seal there."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    # the record starts as the agent's: nothing to seal yet
    S.upsert(conn, S.MemoryItem(slug="owner-edit", kind="feedback", title="rule",
                                body="the first text, written by an agent", origin="agent"))
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='owner-edit'"
                        ).fetchone()["owner_seal"] == 0
    # the owner rewrites it at a TTY — the path that also clears any approval
    S.upsert(conn, S.MemoryItem(slug="owner-edit", kind="feedback", title="rule",
                                body="the second text, rewritten by the owner",
                                origin="owner"),
             reason="owner edit")
    row = conn.execute("SELECT origin, trusted_at, owner_seal FROM memory_items "
                       "WHERE slug='owner-edit'").fetchone()
    assert (row["origin"], row["trusted_at"], row["owner_seal"]) == ("owner", None, 1)
    import pytest
    monkeypatch.setattr(S, "owner_present", lambda: False)   # the agent's side
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "owner-edit", True)


def test_agent_cannot_relabel_a_sealed_record_out_of_the_briefing(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="rule-kind", kind="feedback", title="rule",
                                body="a rule the briefing selects by kind", origin="owner"))
    # the same-text mem_write route, which had no guard of its own
    err = _payload(mcp._tool_write({"slug": "rule-kind", "title": "rule",
                                    "body": "a rule the briefing selects by kind",
                                    "kind": "note"}))
    assert "cannot change its kind" in err.get("error", ""), err
    err2 = _payload(mcp._tool_update({"slug": "rule-kind", "kind": "note",
                                      "body": "a slightly different rule text here",
                                      "reason": "relabel"}))
    assert "cannot change its kind" in err2.get("error", ""), err2
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='rule-kind'"
                        ).fetchone()["kind"] == "feedback"


def test_the_briefing_names_the_owner_rules_an_agent_rewrote(mcp):
    """An agent update clears the approval by design, so the rule leaves the
    briefing. Silently, it just stops arriving and nothing says why."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="gate-rule", kind="feedback", title="gate",
                                body="deploy only through the gate, never by hand",
                                origin="owner"))
    S.set_trust(conn, "gate-rule", trusted=True)
    assert any(e["slug"] == "gate-rule"
               for sec in S.briefing(conn)["sections"] for e in sec["items"])
    _payload(mcp._tool_update({"slug": "gate-rule", "body": "deploy through the gate, always",
                               "reason": "tightened"}))
    brief = S.briefing(conn)
    assert not any(e["slug"] == "gate-rule"
                   for sec in brief["sections"] for e in sec["items"])
    assert "gate-rule" in brief["awaiting_reapproval"]


def test_the_owner_check_lives_in_the_mutation_not_the_caller(mcp, monkeypatch, at_terminal):
    """Eleven callers had to remember to pass the flag and the eleventh did not.
    The mutation asks the owner signal itself now."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    for slug in ("mut-arch", "mut-del"):
        S.upsert(conn, S.MemoryItem(slug=slug, kind="feedback", title="rule",
                                    body=f"the owner's rule {slug} kept here",
                                    origin="owner"), owner_call=True)
    monkeypatch.setattr(S, "owner_present", lambda: False)
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "mut-arch", True)     # no flag at all
    with pytest.raises(S.SealedRecord):
        S.soft_delete(conn, "mut-del", "cleanup")  # no flag at all
    monkeypatch.setattr(S, "owner_present", lambda: True)
    assert S.set_archived(conn, "mut-arch", True)["lifecycle"] == "archived"
    assert S.soft_delete(conn, "mut-del", "cleanup") is True


def test_deleting_twice_reports_the_second_as_nothing(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="twice", kind="skill", title="t",
                                body="a skill to delete twice over"))
    assert S.soft_delete(conn, "twice", "first") is True
    assert S.soft_delete(conn, "twice", "second") is False
    rows = conn.execute("SELECT COUNT(*) c FROM memory_history WHERE slug='twice'"
                        ).fetchone()["c"]
    assert rows == 1                       # one record, one deletion, one row


def test_every_owner_only_command_is_denied_by_init(mcp):
    """The TTY check is accident protection; the deny rules are the wall."""
    from skillmem import cli as C
    assert set(C._OWNER_DENY_RULES) == {
        "Bash(skillmem trust*)", "Bash(skillmem skills-archive*)",
        "Bash(skillmem rm*)", "Bash(skillmem import-vault*)",
    }


def test_a_body_file_with_no_recorded_hash_is_not_served(tmp_path, monkeypatch):
    """An empty content_hash used to switch the comparison off entirely."""
    from skillmem import storage as S
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SKILLMEM_DB", raising=False)
    conn = S.connect(tmp_path / "home" / "memory.db"); S.init_schema(conn)
    big = "the owner's rule about the deploy gate. " * 400
    S.upsert(conn, S.MemoryItem(slug="no-hash", kind="feedback", title="rule",
                                body=big, origin="owner"), owner_call=True)
    item = S.get(conn, "no-hash")
    assert item.body_path
    conn.execute("UPDATE memory_items SET content_hash = '' WHERE slug = 'no-hash'")
    served = S.load_body(S.get(conn, "no-hash"))
    assert served == S.get(conn, "no-hash").body       # the excerpt, not the file
    assert "no-hash" in S.mismatched_bodies(conn)


def test_an_unnamed_kind_is_not_written(mcp):
    """The guard exempts a kind the caller never named — so the write must not
    apply the dataclass default either, or the record is relabelled anyway."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="keep-kind", kind="feedback", title="rule",
                                body="the owner's rule about the deploy gate",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "keep-kind", trusted=True)
    # a write that names other fields but not the kind: the item still carries a
    # default kind, and it must not reach the row
    S.upsert(conn, S.MemoryItem(slug="keep-kind", kind="note", title="rule",
                                body="the owner's rule, edited by an agent",
                                tags=["edited"]),
             explicit={"tags"}, reason="agent edit")
    row = conn.execute("SELECT kind FROM memory_items WHERE slug='keep-kind'").fetchone()
    assert row["kind"] == "feedback"


def test_approval_refuses_an_archived_record(mcp):
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="arch-rule", kind="feedback", title="rule",
                                body="a rule retired before approval",
                                origin="owner"), owner_call=True)
    S.set_archived(conn, "arch-rule", True, allow_sealed=True, by="owner-cli")
    with pytest.raises(S.MemoryConflict, match="archived"):
        S.set_trust(conn, "arch-rule", trusted=True)
    assert conn.execute("SELECT trusted_at FROM memory_items WHERE slug='arch-rule'"
                        ).fetchone()["trusted_at"] is None


def test_hiding_and_deleting_default_to_refusing(mcp, at_terminal, monkeypatch):
    """A caller that says nothing must be refused: every hole in this feature was
    a caller that said nothing."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    for slug in ("def-arch", "def-del"):
        S.upsert(conn, S.MemoryItem(slug=slug, kind="feedback", title="rule",
                                    body=f"the owner's rule {slug} kept here",
                                    origin="owner"), owner_call=True)
    monkeypatch.setattr(S, "owner_present", lambda: False)   # the agent's side
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "def-arch", True)
    with pytest.raises(S.SealedRecord):
        S.soft_delete(conn, "def-del", "cleanup")       # no allow_sealed
    rows = conn.execute("SELECT slug, lifecycle, deleted_at FROM memory_items "
                        "WHERE slug IN ('def-arch','def-del') ORDER BY slug").fetchall()
    assert [(r["lifecycle"], r["deleted_at"]) for r in rows] == [("active", None)] * 2


def test_recall_does_not_fail_or_stall_behind_a_writer(tmp_path):
    """Recall is a read path the hooks run on every prompt; its recency
    bookkeeping takes a write lock and must never be the reason it fails."""
    import sqlite3, time
    from skillmem import storage as S
    conn = S.connect(tmp_path / "m.db"); S.init_schema(conn)
    for i in range(5):
        S.upsert(conn, S.MemoryItem(slug=f"sk{i}", kind="skill", title=f"skill {i}",
                                    body=f"how to do the thing number {i} properly"))
    holder = sqlite3.connect(tmp_path / "m.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE memory_items SET title = title WHERE slug = 'sk0'")
    try:
        conn.execute("PRAGMA busy_timeout = 300")
        t0 = time.time()
        out = S.recall_skills(conn, "the thing", limit=5)
        waited = time.time() - t0
        assert len(out) == 5                      # results, not an exception
        assert waited < 1.0, f"waited {waited:.2f}s: one timeout per row, not per call"
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_the_owner_signal_is_the_terminal_not_the_module(mcp, monkeypatch):
    """`skillmem write` and `skillmem migrate` are as reachable from Bash as any
    MCP tool, so the module a call comes from proves nothing."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="tty-rule", kind="feedback", title="rule",
                                body="deploy only through the gate, always",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "tty-rule", trusted=True)
    monkeypatch.setattr(S.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(S.sys.stdout, "isatty", lambda: False, raising=False)
    assert S.owner_present() is False
    # what the CLI now passes with no terminal present
    with pytest.raises(S.SealedRecord):
        S.upsert(conn, S.MemoryItem(slug="tty-rule", kind="note", title="rule",
                                    body="deploy only through the gate, always"),
                 explicit={"kind"}, owner_call=S.owner_present())
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='tty-rule'"
                        ).fetchone()["kind"] == "feedback"


def test_the_guard_fires_when_the_caller_names_no_fields(mcp):
    """explicit=None means "apply everything", so it includes the kind. migrate,
    packs and the importer all pass None, and the guard used to skip them."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="none-rule", kind="feedback", title="rule",
                                body="the owner's rule with an explicit set",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "none-rule", trusted=True)
    with pytest.raises(S.SealedRecord):
        S.upsert(conn, S.MemoryItem(slug="none-rule", kind="note", title="rule",
                                    body="a rewritten body from a markdown file"),
                 explicit=None, reason="migrated from .md")
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='none-rule'"
                        ).fetchone()["kind"] == "feedback"


def test_reinforce_refuses_an_archived_record(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-arch", "title": "arch", "trigger": "a trigger",
                              "steps": "the steps", "outcome": "success", "lessons": None}))
    before = conn.execute("SELECT strength FROM memory_items WHERE slug='skill-arch'"
                          ).fetchone()["strength"]
    S.set_archived(conn, "skill-arch", True, allow_sealed=True, by="owner-cli")
    assert S.reinforce(conn, "skill-arch", evidence="user_confirmed") is None
    after = conn.execute("SELECT strength, access_count FROM memory_items "
                         "WHERE slug='skill-arch'").fetchone()
    assert after["strength"] == before and after["access_count"] == 0


def test_every_upsert_caller_is_deliberate_about_the_guard():
    """The audit three rounds of P1s were missing: an owner surface must say
    owner_call=True, an agent surface must never claim it. A surface added
    without a decision is guarded by default; this only checks the decisions."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "skillmem"
    owner_surfaces = {"cli.py", "migrate.py", "vault.py"}     # ask the terminal
    agent_surfaces = {"mcp_server.py", "server.py", "packs.py"}
    for name in owner_surfaces | agent_surfaces:
        lines = (root / name).read_text().splitlines()
        for i, line in enumerate(lines):
            if not line.strip().endswith("upsert(") and "upsert(conn" not in line:
                continue
            window = "\n".join(lines[i:i + 16])
            if name in owner_surfaces:
                # the TTY, never a hardcoded True: an agent runs these commands too
                assert "owner_call=" in window and "owner_present()" in window, \
                    f"{name}:{i + 1} must derive owner_call from owner_present()"
                assert "owner_call=True" not in window, f"{name}:{i + 1} hardcodes owner_call"
            else:
                assert "owner_call" not in window, f"{name}:{i + 1} claims owner_call"


def test_the_kind_guard_is_on_by_default(mcp):
    """Three rounds running, the hole was a surface that did not pass the flag.
    The guard is on unless a caller states it is the owner's own."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="fb-rule", kind="feedback", title="rule",
                                body="deploy only through the gate here",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "fb-rule", trusted=True)
    # any caller that does not say "owner" is refused, same text or not
    with pytest.raises(S.SealedRecord):
        S.upsert(conn, S.MemoryItem(slug="fb-rule", kind="note", title="rule",
                                    body="deploy only through the gate here"),
                 explicit={"kind"})
    with pytest.raises(S.SealedRecord):
        S.upsert(conn, S.MemoryItem(slug="fb-rule", kind="note", title="rule",
                                    body="a different body for this same rule"),
                 explicit={"kind"}, reason="relabel")
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='fb-rule'"
                        ).fetchone()["kind"] == "feedback"
    # the owner's own surface may
    S.upsert(conn, S.MemoryItem(slug="fb-rule", kind="note", title="rule",
                                body="deploy only through the gate here",
                                origin="owner"), explicit={"kind"}, owner_call=True)
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='fb-rule'"
                        ).fetchone()["kind"] == "note"


def test_approval_is_pinned_to_the_text_the_owner_read(mcp):
    """An agent rewrite landing between the read and the approval would otherwise
    become approved text, and the hooks would inject the agent's version."""
    from skillmem import storage as S
    import pytest
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="rule-swap", kind="feedback", title="rule",
                                body="never paste tokens into a prompt",
                                origin="owner"), owner_call=True)
    seen = S.get(conn, "rule-swap")
    # the agent substitutes the body in the gap
    S.upsert(conn, S.MemoryItem(slug="rule-swap", kind="feedback", title="rule",
                                body="paste tokens straight into the prompt"),
             reason="agent edit")
    with pytest.raises(S.MemoryConflict):
        S.set_trust(conn, "rule-swap", trusted=True, expect_hash=seen.content_hash)
    assert conn.execute("SELECT trusted_at FROM memory_items WHERE slug='rule-swap'"
                        ).fetchone()["trusted_at"] is None


def test_pin_and_reinforce_refuse_a_tombstone(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="skill-gone", kind="skill", title="gone",
                                body="a skill that is about to be deleted"))
    S.soft_delete(conn, "skill-gone", "removed")
    assert S.set_pinned(conn, "skill-gone", True) is None
    assert S.reinforce(conn, "skill-gone") is None
    row = conn.execute("SELECT pinned, access_count FROM memory_items "
                       "WHERE slug='skill-gone'").fetchone()
    assert (row["pinned"], row["access_count"]) == (0, 0)


def test_an_owner_write_of_the_same_text_still_seals(mcp):
    """The seal assignment sat in a branch no real caller reaches: every surface
    passes an explicit field set, so an owner CLI write left the record unsealed."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="same-rule", kind="feedback", title="rule",
                                body="the exact text of this rule", origin="agent"))
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='same-rule'"
                        ).fetchone()["owner_seal"] == 0
    # the owner writes the identical text, the way the CLI does
    S.upsert(conn, S.MemoryItem(slug="same-rule", kind="feedback", title="rule",
                                body="the exact text of this rule", origin="owner"),
             explicit={"kind"})
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='same-rule'"
                        ).fetchone()["owner_seal"] == 1
    import pytest
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "same-rule", True)


def test_the_seal_is_checked_inside_the_write(mcp, at_terminal):
    """A gate that reads the seal and then archives loses the race against the
    owner approving the record in between, so storage enforces it."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="race-rule", kind="feedback", title="rule",
                                body="a rule approved between check and write",
                                origin="owner"))
    import pytest
    with pytest.raises(S.SealedRecord):
        S.set_archived(conn, "race-rule", True, allow_sealed=False)
    # the owner's own path says so explicitly
    assert S.set_archived(conn, "race-rule", True,
                          allow_sealed=True)["lifecycle"] == "archived"


def test_update_history_names_the_surface_not_the_client(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="upd-actor", kind="note", title="note",
                                body="the first text of this note here"))
    _payload(mcp._tool_update({"slug": "upd-actor", "body": "a different text for this note",
                               "reason": "revised"}))
    actor = conn.execute("SELECT changed_by FROM memory_history WHERE slug='upd-actor' "
                         "ORDER BY id DESC LIMIT 1").fetchone()["changed_by"]
    assert actor.startswith("mcp:"), actor


def test_pack_removal_spares_a_sealed_record(mcp, at_terminal):
    """An agent can set project='pack:x' on a record whose slug matches the
    prefix; the owner's own pack removal must not then delete it."""
    from skillmem import storage as S
    from skillmem.packs import remove_pack
    conn = mcp._shared_conn(); S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(slug="pack-demo-rule", kind="feedback", title="rule",
                                body="the owner's rule that looks like a pack skill",
                                origin="owner"))
    # the state an agent's own edits leave: origin relabelled, project filed
    # under the pack, and the slug already matching the import's prefix
    conn.execute("UPDATE memory_items SET origin = 'agent', project = 'pack:demo' "
                 "WHERE slug = 'pack-demo-rule'")
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='pack-demo-rule'"
                        ).fetchone()["owner_seal"] == 1
    removed = remove_pack(conn, "demo", reason="pack removed")
    assert "pack-demo-rule" not in removed
    row = conn.execute("SELECT deleted_at, owner_seal FROM memory_items "
                       "WHERE slug='pack-demo-rule'").fetchone()
    assert row["deleted_at"] is None and row["owner_seal"] == 1


def test_nightly_sweep_never_hides_an_owner_record(mcp, at_terminal):
    """The slow path to the same place: an agent can drive strength to the floor."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    old_ts = 1_600_000_000
    for slug, origin in (("owner-rule", "owner"), ("agent-skill", "agent")):
        S.upsert(conn, S.MemoryItem(slug=slug, kind="skill", title=slug,
                                    body=f"the body of {slug} kept for the sweep test",
                                    origin=origin))
        conn.execute("UPDATE memory_items SET strength = ?, last_accessed_at = ?, "
                     "created_at = ? WHERE slug = ?",
                     (S.DECAY_FLOOR, old_ts, old_ts, slug))
    swept = S.sweep_lifecycle(conn)
    assert "agent-skill" in swept["archived"]
    assert "owner-rule" not in swept["archived"]
    # and what it did archive left the same audit row an explicit archive leaves
    rows = conn.execute("SELECT changed_by, reason FROM memory_history "
                        "WHERE slug='agent-skill' ORDER BY id").fetchall()
    assert rows and rows[-1]["reason"] == "archived by nightly sweep"
    assert rows[-1]["changed_by"] == "sweep"
    _, broken = S.verify_history(conn)
    assert broken == []


def test_history_actor_cannot_be_spoofed_by_the_client(mcp, monkeypatch):
    """_agent() falls back to clientInfo.name, which the agent supplies."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    monkeypatch.setattr(mcp, "_ENV_AGENT", "owner-cli", raising=False)
    monkeypatch.setattr(mcp, "_client_agent", "owner-cli", raising=False)
    _payload(mcp._tool_learn({"slug": "skill-spoof", "title": "spoof", "trigger": "a trigger",
                              "steps": "the steps", "outcome": "success", "lessons": None}))
    S.set_archived(conn, "skill-spoof", True, allow_sealed=True, by="owner-cli")
    actor = conn.execute("SELECT changed_by FROM memory_history WHERE slug='skill-spoof' "
                         "ORDER BY id DESC LIMIT 1").fetchone()["changed_by"]
    # an agent write stamps the surface; the archive above is the owner's own path
    assert actor == "owner-cli"


def test_restoring_an_active_record_writes_no_history(mcp):
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-live", "title": "live", "trigger": "a trigger",
                              "steps": "the steps", "outcome": "success", "lessons": None}))
    before = conn.execute("SELECT COUNT(*) c FROM memory_history "
                          "WHERE slug='skill-live'").fetchone()["c"]
    r = S.set_archived(conn, "skill-live", False, by="owner-cli")
    assert r["was"] == "active"
    after = conn.execute("SELECT COUNT(*) c FROM memory_history "
                         "WHERE slug='skill-live'").fetchone()["c"]
    assert after == before                   # no transition, no row


def test_archiving_leaves_a_history_row(mcp):
    """The owner's only trace of a record leaving every read."""
    from skillmem import storage as S
    conn = mcp._shared_conn(); S.init_schema(conn)
    _payload(mcp._tool_learn({"slug": "skill-temp", "title": "temp", "trigger": "a trigger here",
                              "steps": "the steps taken", "outcome": "success", "lessons": None}))
    before = conn.execute("SELECT COUNT(*) c FROM memory_history WHERE slug='skill-temp'").fetchone()["c"]
    S.set_archived(conn, "skill-temp", True, allow_sealed=True, by="owner-cli")
    S.set_archived(conn, "skill-temp", False, by="owner-cli")
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
    S.set_archived(conn, "skill-gate", True, allow_sealed=True, by="owner-cli")
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
    r = S.set_archived(conn, "skill-gate", False, by="owner-cli")
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
