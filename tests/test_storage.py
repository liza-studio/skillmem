"""Core storage layer: tx, conflict, TTL, externalize."""

from __future__ import annotations

import pytest

from skillmem import storage as S


def _item(slug, body="hello world", **kw):
    return S.MemoryItem(slug=slug, kind=kw.pop("kind", "note"),
                        title=kw.pop("title", "t"), body=body, **kw)


def test_tx_savepoint_rolls_back_inner_keeps_outer(conn):
    """SAVEPOINT isolation: inner failure rolls back inner, outer commits."""
    with S.tx(conn):
        S.upsert(conn, _item("outer-keep"))
        try:
            with S.tx(conn):
                S.upsert(conn, _item("inner-die"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    slugs = {r[0] for r in conn.execute("SELECT slug FROM memory_items")}
    assert "outer-keep" in slugs
    assert "inner-die" not in slugs


def test_tx_nested_uses_unique_savepoint_names(conn):
    """Triple-nested tx must commit all three rows without SAVEPOINT collision."""
    with S.tx(conn):
        with S.tx(conn):
            with S.tx(conn):
                S.upsert(conn, _item("a"))
            S.upsert(conn, _item("b"))
        S.upsert(conn, _item("c"))
    assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 3


def test_conflict_detection_rejects_near_duplicate(conn):
    """Inclusion overlap > 0.7 should trigger MemoryConflict with JSON payload."""
    body = "правило никогда не врать выдумывать факты только проверенная информация уволят"
    S.upsert(conn, _item("orig", body=body))
    with pytest.raises(S.MemoryConflict) as exc:
        S.upsert(conn, _item("dup", body=body), check_conflicts=True)
    assert "duplicate-candidates:" in str(exc.value)
    assert "orig" in str(exc.value)


def test_conflict_bypass_with_check_disabled(conn):
    """check_conflicts=False must allow near-duplicates through."""
    body = "very similar text content here for overlap test"
    S.upsert(conn, _item("a", body=body))
    S.upsert(conn, _item("b", body=body), check_conflicts=False)
    assert conn.execute("SELECT count(*) FROM memory_items").fetchone()[0] == 2


def test_ttl_freshness_marks_stale(conn):
    """Records past freshness_until must come back tagged 'stale' with day count."""
    import time
    past = int(time.time()) - 86400 * 5
    item = _item("old-record", body="устаревшая информация", freshness_until=past)
    S.upsert(conn, item)
    hits = S.search(conn, "устаревшая")
    assert hits and hits[0]["slug"] == "old-record"
    assert hits[0]["freshness"] == "stale"
    assert hits[0]["stale_days"] == 5


def test_search_results_carry_mcp_contract_keys(conn):
    """mcp_server._tool_search reads these keys directly — KeyError there means
    a broken mem_search tool (regression: 'rank' was dropped in the hybrid rewrite)."""
    S.upsert(conn, _item("first", body="команда выиграла гонку"))
    S.upsert(conn, _item("second", body="гонка прошла на автодроме"))
    hits = S.search(conn, "гонка")
    assert len(hits) == 2
    for key in ("slug", "kind", "title", "project", "rank", "snippet", "updated_at"):
        assert key in hits[0], f"missing {key}"
    assert [h["rank"] for h in hits] == [1, 2]


def test_externalize_long_body(conn, memhome):
    """body > 8KB or kind=document goes to disk; DB keeps an excerpt."""
    long_body = "paragraph text " * 1000  # ~15KB
    S.upsert(conn, _item("big", body=long_body, kind="document"))
    got = S.get(conn, "big")
    assert got.body_path is not None
    assert len(got.body) < len(long_body)         # excerpt only in DB
    assert S.load_body(got) == long_body          # full text from disk
    assert (S.docs_dir() / got.body_path).exists()
