"""Skill lifecycle: active -> stale -> archived (Phase 2)."""

from skillmem import storage as S


def _skill(conn, slug, title, *, strength, age_days):
    S.upsert(conn, S.MemoryItem(slug=slug, kind="skill", title=title,
                                body="trigger\nsteps\noutcome", visibility="public"))
    old = S._now() - age_days * 86400
    conn.execute(
        "UPDATE memory_items SET created_at=?, last_accessed_at=?, strength=? WHERE slug=?",
        (old, None, strength, slug),
    )
    conn.commit()


def test_old_faded_skill_archived(conn):
    _skill(conn, "sk-dead", "ancient unused skill", strength=S.DECAY_FLOOR, age_days=120)
    sweep = S.sweep_lifecycle(conn)
    assert "sk-dead" in sweep["archived"]
    state = conn.execute(
        "SELECT lifecycle FROM memory_items WHERE slug='sk-dead'"
    ).fetchone()[0]
    assert state == "archived"


def test_old_strong_skill_only_stale_not_archived(conn):
    _skill(conn, "sk-strong", "old but still strong skill", strength=1.0, age_days=120)
    sweep = S.sweep_lifecycle(conn)
    assert "sk-strong" in sweep["staled"]
    assert "sk-strong" not in sweep["archived"]


def test_recent_skill_untouched(conn):
    _skill(conn, "sk-fresh", "recently created skill", strength=1.0, age_days=2)
    sweep = S.sweep_lifecycle(conn)
    assert "sk-fresh" not in sweep["staled"]
    assert "sk-fresh" not in sweep["archived"]


def test_archived_excluded_from_recall_and_restorable(conn):
    _skill(conn, "sk-arch", "unique borscht recipe skill token", strength=S.DECAY_FLOOR, age_days=200)
    S.sweep_lifecycle(conn)
    found = [r["slug"] for r in S.search(conn, "borscht recipe token", kind="skill")]
    assert "sk-arch" not in found
    assert S.restore_skill(conn, "sk-arch") is True
    state = conn.execute(
        "SELECT lifecycle FROM memory_items WHERE slug='sk-arch'"
    ).fetchone()[0]
    assert state == "active"
    found2 = [r["slug"] for r in S.search(conn, "borscht recipe token", kind="skill")]
    assert "sk-arch" in found2


def test_lifecycle_counts(conn):
    _skill(conn, "sk-a", "skill a", strength=1.0, age_days=1)
    counts = S.lifecycle_counts(conn)
    assert counts.get("active", 0) >= 1
