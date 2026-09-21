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
             "пайтест BASE_DIR=/root/liza-dev иначе тесты берут чужое дерево")

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
