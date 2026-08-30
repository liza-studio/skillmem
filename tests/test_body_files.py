"""Externalized bodies (>8KB) must stay in step with the DB row.

The body file used to be written inside the write transaction: a failure in a
later statement rolled the row back while the file already held the new text,
leaving excerpt/content_hash/file disagreeing with no error surfaced.
"""

from __future__ import annotations

import pytest

from skillmem import storage as S

BIG_A = "aaaa " * 3000  # comfortably over the 8KB externalize threshold
BIG_B = "bbbb " * 3000


def _item(body: str) -> S.MemoryItem:
    return S.MemoryItem(slug="big-doc", title="big doc", body=body, kind="note")


def test_large_body_is_externalized_and_readable(conn):
    saved = S.upsert(conn, _item(BIG_A))
    assert saved.body_path, "body should have been externalized"
    assert S.load_body(S.get(conn, "big-doc")) == BIG_A


def test_update_replaces_body_file(conn):
    S.upsert(conn, _item(BIG_A))
    S.upsert(conn, _item(BIG_B), reason="rewrite")
    assert S.load_body(S.get(conn, "big-doc")) == BIG_B


def _fail_inside_tx(monkeypatch):
    """Break one statement inside the write transaction, restoring it after.

    NOTE: never call monkeypatch.undo() here — it would also revert the
    `memhome` fixture's SKILLMEM_HOME, pointing docs_dir() at the real home and
    making load_body() silently fall back to the excerpt.
    """
    original = S._replace_links_inner

    def boom(*a, **kw):
        raise RuntimeError("simulated failure inside the write transaction")

    monkeypatch.setattr(S, "_replace_links_inner", boom)
    return lambda: monkeypatch.setattr(S, "_replace_links_inner", original)


def test_failed_update_leaves_old_body_intact(conn, monkeypatch):
    """REGRESSION: a rolled-back update must not leave the new text on disk."""
    S.upsert(conn, _item(BIG_A))

    restore = _fail_inside_tx(monkeypatch)
    with pytest.raises(RuntimeError):
        S.upsert(conn, _item(BIG_B), reason="rewrite", links=["something"])
    restore()

    row = S.get(conn, "big-doc")
    assert row.body_path, "the row must still point at its body file"
    assert S.load_body(row) == BIG_A, "old body must survive a rolled-back write"


def test_failed_insert_leaves_no_stray_file(conn, monkeypatch):
    restore = _fail_inside_tx(monkeypatch)
    with pytest.raises(RuntimeError):
        S.upsert(conn, _item(BIG_A), links=["x"])
    restore()

    assert S.get(conn, "big-doc") is None
    leftovers = [p for p in S.docs_dir().glob("big-doc*") if ".staged-" not in p.name]
    assert leftovers == [], f"unpublished body file leaked: {leftovers}"


def test_no_staged_scratch_files_survive(conn):
    S.upsert(conn, _item(BIG_A))
    S.upsert(conn, _item(BIG_B), reason="rewrite")
    assert list(S.docs_dir().glob("*.staged-*")) == []
