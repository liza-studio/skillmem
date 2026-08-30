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


def test_reinforce_grows_strength(three_skills):
    conn = three_skills
    item = S.get(conn, "skill-nginx")
    assert item.strength == 1.0
    assert item.access_count == 0

    r = S.reinforce(conn, "skill-nginx")
    assert r["strength"] == pytest.approx(1.15, abs=0.01)
    assert r["access_count"] == 1

    r2 = S.reinforce(conn, "skill-nginx")
    assert r2["strength"] == pytest.approx(1.30, abs=0.01)
    assert r2["access_count"] == 2


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


def test_recall_auto_reinforce(three_skills):
    conn = three_skills
    results = S.recall_skills(conn, "nginx", auto_reinforce=True)
    assert results[0]["strength"] > 1.0
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
