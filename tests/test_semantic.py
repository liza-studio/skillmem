"""Semantic (hybrid BM25+vector) recall tests.

Skipped automatically when the embedder is unavailable (no fastembed / no
model / MEM_SEMANTIC=0) so the base suite still runs offline.
"""

import os

import pytest

from skillmem import embed, storage as S

pytestmark = pytest.mark.skipif(
    not embed.available(), reason="embedder unavailable (fastembed/model/MEM_SEMANTIC)"
)


def _skill(conn, slug, title, body):
    S.upsert(
        conn,
        S.MemoryItem(slug=slug, kind="skill", title=title, body=body, visibility="public"),
    )


@pytest.fixture
def bilingual_skills(conn):
    _skill(conn, "skill-deploy", "Deploy skillmem to production server",
           "trigger: new version ready\nsteps: backup db, scp code, restart service")
    _skill(conn, "skill-hosts", "Mac hosts file overrides DNS, cleanup",
           "trigger: site not loading\nsteps: grep host in /etc/hosts, remove, flushcache")
    _skill(conn, "skill-tron", "TRC20 balance from contract balanceOf",
           "trigger: wallet balance wrong\nsteps: read balanceOf on contract not accounts")
    return conn


def test_cross_lingual_ru_query_finds_en_skill(bilingual_skills):
    """A Russian query must surface the English-titled deploy skill (BM25 can't)."""
    res = S.recall_skills(bilingual_skills, "деплой на сервер бэкап", limit=3, auto_reinforce=False)
    slugs = [r["slug"] for r in res]
    assert "skill-deploy" in slugs


def test_irrelevant_query_returns_empty(bilingual_skills):
    """Vector nearest-neighbour must not leak unrelated skills below threshold."""
    res = S.recall_skills(bilingual_skills, "рецепт борща квантовая физика", auto_reinforce=False)
    assert res == []


def test_embeddings_written_on_upsert(bilingual_skills):
    n = bilingual_skills.execute(
        "SELECT COUNT(*) FROM memory_items WHERE embedding IS NOT NULL"
    ).fetchone()[0]
    assert n == 3


def test_reindex_is_idempotent(bilingual_skills):
    res = S.reindex_embeddings(bilingual_skills, only_missing=False)
    assert res["updated"] == 3


def test_semantic_disabled_falls_back_to_bm25(bilingual_skills, monkeypatch):
    """With MEM_SEMANTIC=0 the vector path is off but lexical recall still works."""
    monkeypatch.setenv("MEM_SEMANTIC", "0")
    embed._model.cache_clear()
    try:
        assert S._vector_ids(bilingual_skills, "deploy", kind="skill") == []
        res = S.recall_skills(bilingual_skills, "deploy server", limit=2, auto_reinforce=False)
        assert any(r["slug"] == "skill-deploy" for r in res)
    finally:
        embed._model.cache_clear()
