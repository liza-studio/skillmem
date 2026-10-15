"""bench/longmemeval.py stores each turn with its session's dataset date.

Before, every turn was born "now", so a ranking signal that reads
created_at/updated_at could not move a single LongMemEval number.
"""
import importlib.util
from pathlib import Path

from skillmem import storage as S

_spec = importlib.util.spec_from_file_location(
    "bench_longmemeval", Path(__file__).resolve().parents[1] / "bench" / "longmemeval.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)

RECORD = {
    "question_id": "q1",
    "question": "what did I buy first?",
    "haystack_session_ids": ["late", "early"],
    "haystack_dates": ["2023/05/20 (Sat) 10:00", "2023/05/01 (Mon) 09:30"],
    "haystack_sessions": [
        [{"role": "user", "content": "I bought a kayak", "has_answer": False},
         {"role": "assistant", "content": "Nice kayak"}],
        [{"role": "user", "content": "I bought a tent", "has_answer": True}],
    ],
}


def _stamps(tmp_path, **kw):
    conn = S.connect(tmp_path / "b.db")
    S.init_schema(conn)
    B._populate(conn, RECORD, **kw)
    return {r["slug"]: (r["created_at"], r["updated_at"])
            for r in conn.execute("SELECT slug, created_at, updated_at FROM memory_items")}


def test_turns_carry_their_session_date(tmp_path):
    got = _stamps(tmp_path)
    early = B._session_epoch("2023/05/01 (Mon) 09:30")
    late = B._session_epoch("2023/05/20 (Sat) 10:00")
    assert got["q1-early-t0"] == (early, early)
    assert got["q1-late-t0"] == (late, late)
    assert got["q1-late-t1"] == (late + 1, late + 1)   # turn order kept inside a session
    assert got["q1-early-t0"][0] < got["q1-late-t0"][0]


def test_no_dates_keeps_the_old_behaviour(tmp_path):
    got = _stamps(tmp_path, dates=False)
    assert all(c > B._session_epoch("2026/01/01 (Thu) 00:00") for c, _ in got.values())
