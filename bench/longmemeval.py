"""LongMemEval retrieval benchmark harness for skillmem.

The "oracle" subset (~15 MB) ships only the supporting sessions for each
question, so we measure pure retrieval: can FTS5+Snowball-stemmed BM25 surface
the right turn for the right question? No model in the loop — this isolates
the memory layer's behaviour.

Metrics:
    hit@K   — at least one ``has_answer=True`` turn appears in the top-K
    mrr     — mean reciprocal rank of the first supporting turn
Reported overall and broken down by question_type.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from skillmem import storage as S


def _load(dataset: Path) -> list[dict]:
    with dataset.open(encoding="utf-8") as f:
        return json.load(f)


def _populate(conn, record: dict) -> set[str]:
    """Insert every turn of the supporting sessions; return slugs that contain the answer."""
    qid = record["question_id"]
    supporting: set[str] = set()
    session_ids = record.get("haystack_session_ids") or []
    sessions = record.get("haystack_sessions") or []

    for sidx, session in enumerate(sessions):
        sid = session_ids[sidx] if sidx < len(session_ids) else f"s{sidx}"
        for tidx, turn in enumerate(session):
            slug = f"{qid}-{sid}-t{tidx}"
            body = f"{turn['role']}: {turn['content']}"
            item = S.MemoryItem(
                slug=slug,
                kind="document",
                title=f"{sid} turn {tidx}",
                body=body,
            )
            S.upsert(conn, item, force=True, check_conflicts=False)
            if turn.get("has_answer"):
                supporting.add(slug)
    return supporting


def _evaluate_one(conn, record: dict, k: int) -> tuple[int, float]:
    supporting = _populate(conn, record)
    if not supporting:
        return -1, 0.0  # nothing to find — skip
    hits = S.search(conn, record["question"], limit=k)
    if not hits:
        return 0, 0.0
    hit_at_k = 0
    rr = 0.0
    for rank, h in enumerate(hits, start=1):
        if h["slug"] in supporting:
            hit_at_k = 1
            rr = 1.0 / rank
            break
    return hit_at_k, rr


def run(dataset: Path, *, sample: int | None, k: int) -> dict:
    records = _load(dataset)
    if sample:
        records = records[:sample]

    by_type: dict[str, list[tuple[int, float]]] = defaultdict(list)
    overall: list[tuple[int, float]] = []
    skipped = 0
    durations: list[float] = []

    for rec in records:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bench.db"
            conn = S.connect(db)
            S.init_schema(conn)
            t0 = time.perf_counter()
            hit, rr = _evaluate_one(conn, rec, k)
            durations.append(time.perf_counter() - t0)
            conn.close()
        if hit < 0:
            skipped += 1
            continue
        by_type[rec.get("question_type", "?")].append((hit, rr))
        overall.append((hit, rr))

    def _report(rows: list[tuple[int, float]]) -> dict:
        if not rows:
            return {"n": 0, f"hit@{k}": None, "mrr": None}
        return {
            "n": len(rows),
            f"hit@{k}": round(sum(h for h, _ in rows) / len(rows), 3),
            "mrr": round(sum(r for _, r in rows) / len(rows), 3),
        }

    return {
        "k": k,
        "dataset": str(dataset),
        "sampled": len(records),
        "evaluated": len(overall),
        "skipped_no_support": skipped,
        "median_seconds_per_query": round(statistics.median(durations) or 0, 4),
        "overall": _report(overall),
        "by_question_type": {qt: _report(rows) for qt, rows in sorted(by_type.items())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="bench-longmemeval")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path(__file__).resolve().parent / "longmemeval_oracle.json",
    )
    parser.add_argument("--sample", type=int, default=100, help="0 for full set")
    parser.add_argument("-k", type=int, default=5)
    args = parser.parse_args()
    sample = args.sample if args.sample > 0 else None
    report = run(args.dataset, sample=sample, k=args.k)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
