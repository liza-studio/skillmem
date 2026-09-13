"""Tests for self-improving skills (v0.4.0): reinforce, decay, recall."""

import time
import pytest

from skillmem import storage as S


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "test.db"
    c = S.connect(db)
    S.init_schema(c)
    return c


@pytest.fixture
def three_skills(conn):
    skills = [
        S.MemoryItem(slug="skill-nginx", kind="skill", title="Deploy Nginx",
                      body="trigger: need nginx\nsteps: install\noutcome: success",
                      visibility="public"),
        S.MemoryItem(slug="skill-sqlite", kind="skill", title="Fix SQLite locks",
                      body="trigger: database locked\nsteps: WAL mode\noutcome: success",
                      visibility="public"),
        S.MemoryItem(slug="skill-fal", kind="skill", title="Generate images fal.ai",
                      body="trigger: need image\nsteps: FLUX schnell\noutcome: success",
                      visibility="public"),
    ]
    for s in skills:
        S.upsert(conn, s)
    return conn


def test_schema_v5_columns(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memory_items)")}
    assert "access_count" in cols
    assert "last_accessed_at" in cols


def test_reinforce_grows_strength_on_outside_evidence(three_skills):
    """Only a signal from outside the agent's own judgement raises strength."""
    conn = three_skills
    item = S.get(conn, "skill-nginx")
    assert item.strength == 1.0
    assert item.access_count == 0

    r = S.reinforce(conn, "skill-nginx", evidence="test_passed")
    assert r["strength"] == pytest.approx(1.15, abs=0.01)
    assert r["access_count"] == 1
    assert r["confirmed_count"] == 1

    r2 = S.reinforce(conn, "skill-nginx", evidence="user_confirmed")
    assert r2["strength"] == pytest.approx(1.30, abs=0.01)
    assert r2["access_count"] == 2
    assert r2["confirmed_count"] == 2


def test_self_report_does_not_grow_strength(three_skills):
    """An agent declaring its own skill useful must not reinforce its mistake."""
    conn = three_skills
    r = S.reinforce(conn, "skill-nginx")          # default: self_report
    assert r["strength"] == pytest.approx(1.0, abs=0.001)
    assert r["access_count"] == 1                 # recency still refreshed
    assert r["confirmed_count"] == 0


def test_failure_weakens_skill(three_skills):
    """A skill followed by a failed task loses ground."""
    conn = three_skills
    S.reinforce(conn, "skill-nginx", evidence="test_passed")
    r = S.reinforce(conn, "skill-nginx", evidence="failure")
    assert r["strength"] == pytest.approx(1.15 * S.FAILURE_FACTOR, abs=0.01)
    assert r["failure_count"] == 1
    assert r["confirmed_count"] == 1              # past confirmation is not erased


def test_unknown_evidence_is_rejected(three_skills):
    """A typo in the evidence must fail loudly, not silently reinforce."""
    with pytest.raises(ValueError):
        S.reinforce(three_skills, "skill-nginx", evidence="looks_fine")


def test_pinned_skill_never_decays_or_archives(three_skills):
    """The rule that matters because it is rarely needed must not fade."""
    conn = three_skills
    assert S.set_pinned(conn, "skill-nginx", True)["pinned"] is True

    conn.execute("UPDATE memory_items SET strength = ?, last_accessed_at = 0 "
                 "WHERE slug = ?", (S.DECAY_FLOOR, "skill-nginx"))
    decayed = [d["slug"] for d in S.decay_stale(conn, days_threshold=0)]
    assert "skill-nginx" not in decayed        # pinned: sat out the sweep
    assert decayed                             # the unpinned ones still decayed

    swept = S.sweep_lifecycle(conn)
    assert "skill-nginx" not in swept["archived"]
    assert "skill-nginx" not in swept["staled"]
    assert S.get(conn, "skill-nginx").pinned is True

    # Unpinning puts it back under the ordinary rules.
    S.set_pinned(conn, "skill-nginx", False)
    assert "skill-nginx" in S.sweep_lifecycle(conn)["archived"]


def test_reinforce_caps_at_max(conn):
    item = S.MemoryItem(slug="skill-capped", kind="skill", title="test",
                         body="test", visibility="public")
    S.upsert(conn, item)
    for _ in range(20):
        S.reinforce(conn, "skill-capped")
    final = S.get(conn, "skill-capped")
    assert final.strength <= S.STRENGTH_CAP


def test_reinforce_nonexistent_returns_none(conn):
    assert S.reinforce(conn, "does-not-exist") is None


def test_decay_reduces_strength(three_skills):
    conn = three_skills
    old_ts = int(time.time()) - 30 * 86400
    conn.execute("UPDATE memory_items SET last_accessed_at = ? WHERE slug = ?",
                 (old_ts, "skill-fal"))
    decayed = S.decay_stale(conn, days_threshold=14)
    slugs = [d["slug"] for d in decayed]
    assert "skill-fal" in slugs
    item = S.get(conn, "skill-fal")
    assert item.strength == pytest.approx(0.85, abs=0.01)


def test_decay_skips_recently_used(three_skills):
    conn = three_skills
    S.reinforce(conn, "skill-nginx")
    decayed = S.decay_stale(conn, days_threshold=14)
    slugs = [d["slug"] for d in decayed]
    assert "skill-nginx" not in slugs


def test_decay_respects_floor(conn):
    item = S.MemoryItem(slug="skill-weak", kind="skill", title="weak",
                         body="test", visibility="public")
    S.upsert(conn, item)
    conn.execute("UPDATE memory_items SET strength = 0.06, last_accessed_at = 1 WHERE slug = ?",
                 ("skill-weak",))
    decayed = S.decay_stale(conn, days_threshold=0)
    assert len(decayed) == 1
    assert decayed[0]["new_strength"] >= S.DECAY_FLOOR


def test_recall_finds_relevant_skill(three_skills):
    conn = three_skills
    results = S.recall_skills(conn, "nginx deploy", auto_reinforce=False)
    assert len(results) >= 1
    assert results[0]["slug"] == "skill-nginx"


def test_recall_auto_reinforce_refreshes_recency_only(three_skills):
    """Retrieval is not evidence: it refreshes recency without raising strength."""
    conn = three_skills
    results = S.recall_skills(conn, "nginx", auto_reinforce=True)
    assert results[0]["strength"] == pytest.approx(1.0, abs=0.001)
    assert results[0]["access_count"] == 1


def test_recall_empty_for_irrelevant(three_skills):
    conn = three_skills
    results = S.recall_skills(conn, "quantum physics", auto_reinforce=False)
    assert len(results) == 0


def test_stats_includes_skills(three_skills):
    conn = three_skills
    st = S.stats(conn)
    assert "skills" in st
    assert st["skills"] == 3


def test_migration_from_v8_adds_columns_without_losing_rows(conn):
    """An existing database picks up the v9 columns in place, data intact."""
    item = S.MemoryItem(slug="skill-old", kind="skill", title="pre-migration",
                        body="written before v9", visibility="public")
    S.upsert(conn, item)

    # Roll the database back to what v8 looked like.
    for col in ("pinned", "confirmed_count", "failure_count"):
        conn.execute(f"ALTER TABLE memory_items DROP COLUMN {col}")
    conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")

    S.init_schema(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_items)")}
    assert {"pinned", "confirmed_count", "failure_count"} <= cols
    assert conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == str(S.CURRENT_SCHEMA_VERSION)

    survived = S.get(conn, "skill-old")
    assert survived.body == "written before v9"
    assert survived.pinned is False          # existing rows default to unpinned
