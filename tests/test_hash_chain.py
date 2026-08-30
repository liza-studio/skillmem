"""SHA256 hash-chain over memory_history (borrowed from NOM §6)."""

from __future__ import annotations

import unicodedata

from skillmem import storage as S


def test_chain_intact_after_normal_updates(conn):
    """3 updates in a row should produce 3 chain-linked history rows that verify."""
    S.upsert(conn, S.MemoryItem(slug="x", kind="note", title="v1", body="b1"))
    S.upsert(conn, S.MemoryItem(slug="x", kind="note", title="v2", body="b2"),
             reason="iterate")
    S.upsert(conn, S.MemoryItem(slug="x", kind="note", title="v3", body="b3"),
             reason="polish")
    checked, breaks = S.verify_history(conn)
    assert checked == 2  # 2 updates → 2 history rows
    assert breaks == []


def test_chain_detects_tampered_row(conn):
    """Hand-editing a history row must surface as a chain break on verify."""
    S.upsert(conn, S.MemoryItem(slug="y", kind="note", title="v1", body="b1"))
    S.upsert(conn, S.MemoryItem(slug="y", kind="note", title="v2", body="b2"),
             reason="legit update")
    # Forge the reason field directly
    conn.execute("UPDATE memory_history SET reason='FORGED' WHERE id=1")
    _, breaks = S.verify_history(conn)
    assert len(breaks) >= 1
    assert breaks[0].slug == "y"


def test_chain_break_propagates_downstream(conn):
    """Forging row N's self_hash must surface as breaks on BOTH N and N+1.

    We simulate the realistic attack — attacker patches a row's stored hash to
    cover for a content change. The next row's prev_hash now refers to the
    pre-tamper hash, so it no longer chains to the (new) actual hash.
    """
    for i in range(4):
        S.upsert(conn, S.MemoryItem(slug="z", kind="note", title=f"v{i}", body=f"b{i}"),
                 reason=f"iter{i}" if i else None)
    # Forge the *stored hash* of row 2 — pretend attacker recalculated after a body edit
    conn.execute("UPDATE memory_history SET self_hash='deadbeef' || substr(self_hash, 9) WHERE id=2")
    _, breaks = S.verify_history(conn)
    broken_ids = {b.row_id for b in breaks}
    # Row 2 (forged) AND row 3 (now points to the wrong prev_hash) must both break.
    assert 2 in broken_ids and 3 in broken_ids, f"expected propagation; got {broken_ids}"


def test_chain_hash_is_unicode_normalised(conn):
    """'café' written in NFC and NFD must produce identical chain hashes."""
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    assert nfc != nfd  # sanity: they're different code-point sequences
    payload_nfc = {"slug": "x", "body": nfc, "n": 1}
    payload_nfd = {"slug": "x", "body": nfd, "n": 1}
    assert S._chain_hash(None, payload_nfc) == S._chain_hash(None, payload_nfd)
