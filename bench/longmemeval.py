"""LongMemEval retrieval benchmark harness for skillmem.

The "oracle" subset (~15 MB) ships only the supporting sessions for each
question, so we measure pure retrieval: can FTS5+Snowball-stemmed BM25 surface
the right turn for the right question? No model in the loop — this isolates
the memory layer's behaviour.

Metrics:
    hit@K   — at least one ``has_answer=True`` turn appears in the top-K
    mrr     — mean reciprocal rank of the first supporting turn
Reported overall and broken down by question_type.

Every turn is stored with the date its session carries in the dataset
(``haystack_dates``, plus one second per turn so turns keep their order).
Without that every record was born "now", and any ranking signal that reads
created_at/updated_at was invisible to this benchmark. ``--no-dates``
reproduces the old behaviour.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from skillmem import storage as S


def _load(dataset: Path) -> list[dict]:
    with dataset.open(encoding="utf-8") as f:
        return json.load(f)


def _session_epoch(stamp: str) -> int:
    """'2023/04/10 (Mon) 17:50' -> unix seconds (UTC; only the order matters)."""
    day, _, clock = stamp.partition(") ")
    dt = datetime.strptime(f"{day.split(' (')[0]} {clock}", "%Y/%m/%d %H:%M")
    return int(dt.replace(tzinfo=timezone.utc).timestamp())


def _populate(conn, record: dict, *, dates: bool = True) -> set[str]:
    """Insert every turn of the supporting sessions; return slugs that contain the answer."""
    qid = record["question_id"]
    supporting: set[str] = set()
    session_ids = record.get("haystack_session_ids") or []
    sessions = record.get("haystack_sessions") or []
    stamps = record.get("haystack_dates") or []

    for sidx, session in enumerate(sessions):
        sid = session_ids[sidx] if sidx < len(session_ids) else f"s{sidx}"
        base = _session_epoch(stamps[sidx]) if dates and sidx < len(stamps) else None
        for tidx, turn in enumerate(session):
            slug = f"{qid}-{sid}-t{tidx}"
            body = f"{turn['role']}: {turn['content']}"
            item = S.MemoryItem(
                slug=slug,
                kind="document",
                title=f"{sid} turn {tidx}",
                body=body,
            )
            if base is None:
                S.upsert(conn, item, force=True, check_conflicts=False)
            else:
                item.created_at = item.updated_at = base + tidx
                S.upsert(conn, item, force=True, check_conflicts=False,
                         explicit=("created_at", "updated_at"))
            if turn.get("has_answer"):
                supporting.add(slug)
    return supporting


def _evaluate_one(conn, record: dict, k: int, *, dates: bool = True) -> tuple[int, float]:
    supporting = _populate(conn, record, dates=dates)
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


def run(dataset: Path, *, sample: int | None, k: int, dates: bool = True) -> dict:
    records = _load(dataset)
    if sample:
        records = records[:sample]

    by_type: dict[str, list[tuple[int, float]]] = defaultdict(list)
    overall: list[tuple[int, float]] = []
    per_question: dict[str, list] = {}
    skipped = 0
    durations: list[float] = []

    for rec in records:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bench.db"
            conn = S.connect(db)
            S.init_schema(conn)
            t0 = time.perf_counter()
            hit, rr = _evaluate_one(conn, rec, k, dates=dates)
            durations.append(time.perf_counter() - t0)
            conn.close()
        if hit < 0:
            skipped += 1
            continue
        per_question[rec["question_id"]] = [hit, round(rr, 4)]
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
        "dates": dates,
        "dataset": str(dataset),
        "sampled": len(records),
        "evaluated": len(overall),
        "skipped_no_support": skipped,
        "median_seconds_per_query": round(statistics.median(durations) or 0, 4),
        "overall": _report(overall),
        "by_question_type": {qt: _report(rows) for qt, rows in sorted(by_type.items())},
        "per_question": per_question,
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
    parser.add_argument("--no-dates", action="store_true",
                        help="store every turn as born now (the pre-fix behaviour)")
    args = parser.parse_args()
    sample = args.sample if args.sample > 0 else None
    report = run(args.dataset, sample=sample, k=args.k, dates=not args.no_dates)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
