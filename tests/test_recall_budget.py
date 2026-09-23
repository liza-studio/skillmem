"""The recall composer's budget: one section must not starve the other.

Sections used to be filled in order, so feedback took the whole budget and the
skills section — header, rows and all — was dropped. On a real corpus that was
the default, not an edge case: 63% of everything injected was feedback, and a
question whose answer was the top-ranked skill came back as generic rules.
"""

from __future__ import annotations

from pathlib import Path

from skillmem import hooks as H
from skillmem import storage as S


def _trusted(conn, slug: str, kind: str, title: str, body: str) -> None:
    S.upsert(conn, S.MemoryItem(slug=slug, kind=kind, title=title, body=body),
             owner_call=True)
    S.set_trust(conn, slug, trusted=True)


def test_feedback_cannot_eat_the_whole_recall_budget(memhome: Path) -> None:
    conn = S.connect(memhome / "b.db")
    S.init_schema(conn)
    for i in range(4):
        _trusted(conn, f"feedback-bulky-{i}", "feedback",
                 f"Правило про pytest номер {i}",
                 "пайтест " + ("очень длинное правило про pytest " * 40))
    _trusted(conn, "skill-the-answer", "skill",
             "pytest в liza-dev: выставить BASE_DIR",
             "пайтест " + ("BASE_DIR=/root/liza-dev иначе тесты берут чужое дерево " * 8))

    out = H._recall_sections(
        conn, "pytest", seen=set(), skills_limit=2, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SKILLS:",
        budget=1500)
    assert "skill-the-answer" in out, out[:400]
    assert len(out) <= 1500


def test_a_lone_section_still_gets_the_whole_budget(memhome: Path) -> None:
    """Reserving a share must not starve a section when the other is empty."""
    conn = S.connect(memhome / "c.db")
    S.init_schema(conn)
    for i in range(3):
        _trusted(conn, f"skill-only-{i}", "skill", f"Скилл {i}",
                 "деплой " + ("подробности про деплой " * 30))
    out = H._recall_sections(
        conn, "деплой", seen=set(), skills_limit=3, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SKILLS:",
        budget=1500)
    assert out.count("- [skill-only-") >= 2, out[:300]


def _slugs(text: str) -> list[str]:
    import re
    return re.findall(r"- \[([^\]]+)\]", text)


def test_a_record_is_never_delivered_twice(memhome: Path) -> None:
    """0.11.2 emitted a section from its share and then re-emitted the WHOLE
    section to spend the leftover, so delivered rows came back a second time."""
    conn = S.connect(memhome / "d.db")
    S.init_schema(conn)
    _trusted(conn, "feedback-one", "feedback", "Правило про деплой",
             "деплой " + ("подробности правила " * 30))
    for i in range(3):
        _trusted(conn, f"skill-dep-{i}", "skill", f"Скилл деплоя {i}",
                 "деплой " + ("шаги выката " * 30))
    out = H._recall_sections(
        conn, "деплой", seen=set(), skills_limit=3, fb_limit=3,
        body_chars=400, fb_header="### FB:", skills_header="### SK:",
        budget=1500)
    got = _slugs(out)
    assert len(got) == len(set(got)), f"дубль в выдаче: {got}"


def _in_order_reference(header_rows, budget, lines):
    """The v0.11.1 algorithm, transcribed: fill sections in order, drop a
    section whole when it does not fit, never cut a row in half."""
    used, out = 0, []
    for header, rows in header_rows:
        rows = list(rows)
        while rows:
            text = header + "\n" + lines(rows)
            if used + len(text) + 2 <= budget:
                out.extend(r["slug"] for r in rows)
                used += len(text) + 2
                break
            rows = rows[:-1]
    return out


def test_never_fewer_distinct_records_than_plain_in_order(memhome: Path) -> None:
    """The split exists to deliver MORE. Compared against the real v0.11.1
    algorithm, not against a proxy: the previous version of this test asserted
    only uniqueness and length, so it stayed green while the invariant it is
    named for was false in 8% of recalls."""
    import random

    from skillmem import hooks as HH

    rnd = random.Random(20260922)
    conn = S.connect(memhome / "e.db")
    S.init_schema(conn)

    def lines(rows):
        return "\n".join(
            f"- [{r['slug']}] {r.get('title','')}\n  {(r.get('body') or '')[:400]}"
            for r in rows)

    worse = 0
    for case in range(60):
        fb = [{"slug": f"f{case}-{i}", "title": f"Правило {i}",
               "body": "деплой " * rnd.randint(5, 60)} for i in range(3)]
        sk = [{"slug": f"s{case}-{i}", "title": f"Скилл {i}",
               "body": "деплой " * rnd.randint(5, 60)} for i in range(2)]
        budget = rnd.choice([600, 900, 1200, 1500, 2000])
        ref = _in_order_reference([("### FB:", fb), ("### SK:", sk)], budget, lines)

        plan = HH.plan_budget([("### FB:", fb), ("### SK:", sk)],
                              limit=budget,
                              render=lambda h, rows: h + "\n" + lines(rows))
        got = [r["slug"] for _h, rows in plan for r in rows]
        assert len(got) == len(set(got)), (case, got)
        if len(got) < len(ref):
            worse += 1
    assert worse == 0, f"{worse} of 60 cases deliver fewer records than v0.11.1"


def test_budget_edges_do_not_explode(memhome: Path) -> None:
    conn = S.connect(memhome / "f.db")
    S.init_schema(conn)
    _trusted(conn, "feedback-x", "feedback", "Правило", "деплой " * 60)
    _trusted(conn, "skill-y", "skill", "Скилл", "деплой " * 60)
    for budget in (0, 1, 10, 40, 120):
        out = H._recall_sections(
            conn, "деплой", seen=set(), skills_limit=2, fb_limit=2,
            body_chars=400, fb_header="### FB:", skills_header="### SK:",
            budget=budget)
        assert len(out) <= budget or out == "", (budget, len(out))
