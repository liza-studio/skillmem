"""Property tests for the recall composer — the reviewers' probes, kept.

Two review rounds on 0.11.3 each ran the allocator over 20,000 random
corpora and found, in the code they replaced, 19% of prompts carrying fewer
records than v0.11.1 and 22% carrying a duplicate. Those probes lived in a
scratch directory. They live here now, seeded and sized to run in seconds,
so the next change to this path is measured before it is argued about.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from skillmem import hooks as H
from skillmem import storage as S
from tests.test_recall_budget import _in_order_reference

# The two shipped parameter sets (hooks.auto_recall / hooks.tool_recall).
PARAMS = {
    "auto-recall": dict(budget=1500, fb_limit=3, skills_limit=2, body_chars=400),
    "tool-recall": dict(budget=1000, fb_limit=2, skills_limit=2, body_chars=250),
}
FB_HEADER = "### Relevant feedback:"
SK_HEADER = "### Relevant skills (how this was done before):"


def _lines(body_chars: int):
    def lines(rows):
        return "\n".join(
            f"- [{r['slug']}] {r.get('title', '')}\n  {(r.get('body') or '')[:body_chars]}"
            for r in rows)
    return lines


def _corpus(rnd: random.Random, case: int, fb_n: int, sk_n: int):
    fb = [{"slug": f"f{case}-{i}", "title": "Правило " * rnd.randint(1, 6),
           "body": "деплой " * rnd.randint(3, 70)} for i in range(fb_n)]
    sk = [{"slug": f"s{case}-{i}", "title": "Скилл " * rnd.randint(1, 6),
           "body": "деплой " * rnd.randint(3, 70)} for i in range(sk_n)]
    return fb, sk


@pytest.mark.parametrize("hook", sorted(PARAMS))
def test_plan_budget_properties_at_shipped_parameters(hook: str) -> None:
    p = PARAMS[hook]
    rnd = random.Random(20260922 + len(hook))
    lines = _lines(p["body_chars"])

    def render(h, rows):
        return h + "\n" + lines(rows)

    over = dup = fewer = nondet = 0
    for case in range(4000):
        fb, sk = _corpus(rnd, case, rnd.randint(0, p["fb_limit"]),
                         rnd.randint(0, p["skills_limit"]))
        sections = [(FB_HEADER, fb), (SK_HEADER, sk)]
        plan = H.plan_budget(sections, limit=p["budget"], render=render)
        again = H.plan_budget(sections, limit=p["budget"], render=render)
        got = [r["slug"] for _h, rows in plan for r in rows]
        spent = sum(len(render(h, rows)) + 2 for h, rows in plan)
        ref = _in_order_reference(sections, p["budget"], lines)
        over += spent > p["budget"]
        dup += len(got) != len(set(got))
        fewer += len(got) < len(ref)
        nondet += plan != again
    assert (over, dup, fewer, nondet) == (0, 0, 0, 0), {
        "over_budget": over, "duplicate": dup,
        "fewer_than_v0.11.1": fewer, "nondeterministic": nondet}


@pytest.mark.parametrize("hook", sorted(PARAMS))
def test_composer_properties_with_unapproved_rows(hook: str, memhome: Path) -> None:
    """End to end through _recall_sections, half the corpus unapproved: budget
    held, no slug twice, unapproved rows always inside a balanced frame, never
    an approved row beyond its limit, and never fewer APPROVED rows than the
    in-order fill of the same approved candidates."""
    p = PARAMS[hook]
    rnd = random.Random(20260923 + len(hook))
    slug_re = re.compile(r"^- \[([^\]\s]+)\]", re.M)

    # Enough rows on each side to exceed the limits, and bodies long enough
    # to press on the budget: with five short rows the 0.11.2 composer that
    # duplicated a record passed this test — a property test that is green
    # on the bug it exists for is worth nothing.
    for case in range(60):
        conn = S.connect(memhome / f"{hook}-{case}.db")
        S.init_schema(conn)
        approved: list[str] = []
        n_fb = rnd.randint(1, p["fb_limit"] + 3)
        n_sk = rnd.randint(1, p["skills_limit"] + 3)
        for kind, n in (("feedback", n_fb), ("skill", n_sk)):
            for i in range(n):
                slug = f"{kind}-{case}-{i}"
                S.upsert(conn, S.MemoryItem(
                    slug=slug, kind=kind, title="деплой " * rnd.randint(1, 8),
                    body="деплой " * rnd.randint(20, 90)), owner_call=True)
                if rnd.random() < 0.7:
                    S.set_trust(conn, slug, trusted=True)
                    approved.append(slug)
        out = H._recall_sections(
            conn, "деплой", seen=set(), skills_limit=p["skills_limit"],
            fb_limit=p["fb_limit"], body_chars=p["body_chars"],
            fb_header=FB_HEADER, skills_header=SK_HEADER, budget=p["budget"])
        conn.close()

        assert len(out) <= p["budget"], (hook, case, len(out))
        slugs = slug_re.findall(out)
        assert len(slugs) == len(set(slugs)), (hook, case, slugs)
        assert out.count(H.UNTRUSTED_OPEN) == out.count(H.UNTRUSTED_CLOSE) <= 1
        framed = ""
        if H.UNTRUSTED_OPEN in out:
            framed = out[out.index(H.UNTRUSTED_OPEN):out.index(H.UNTRUSTED_CLOSE)]
        for slug in slugs:
            in_frame = f"[{slug}]" in framed
            assert in_frame == (slug not in approved), (hook, case, slug)
        trusted_fb = [s for s in slugs if s.startswith("feedback") and s in approved]
        trusted_sk = [s for s in slugs if s.startswith("skill") and s in approved]
        assert len(trusted_fb) <= p["fb_limit"] and len(trusted_sk) <= p["skills_limit"]

        # The floor. Without it an empty composer passed everything above.
        # Rebuild the approved candidates the composer was handed and render
        # them exactly as it does (hooks._one_line), then the in-order fill
        # of v0.11.1 is the least it may deliver.
        conn = S.connect(memhome / f"{hook}-{case}.db")
        ok = lambda m: m.get("trusted_at") is not None  # noqa: E731
        cand_fb = S.search(conn, "деплой", kind="feedback", limit=p["fb_limit"])
        cand_sk = S.recall_skills(conn, "деплой", limit=p["skills_limit"],
                                  auto_reinforce=False)
        cand_fb = [r for r in cand_fb if ok(r)]
        cand_sk = [r for r in cand_sk if ok(r) and r.get("strength", 0.0) >= 0.0]
        conn.close()

        def lines(rows):
            return "\n".join(
                f"- [{r['slug']}] {H._one_line(r.get('title', ''), 200)}\n"
                f"  {H._one_line(r.get('body', ''), p['body_chars'])}" for r in rows)
        floor = _in_order_reference([(FB_HEADER, cand_fb), (SK_HEADER, cand_sk)],
                                    p["budget"], lines)
        assert len(trusted_fb) + len(trusted_sk) >= len(floor), (
            hook, case, "delivered", trusted_fb + trusted_sk, "in-order floor", floor)
