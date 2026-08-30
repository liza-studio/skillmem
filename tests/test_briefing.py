"""Briefing / `skillmem inject` — the SessionStart hook payload."""

from __future__ import annotations

import json

from click.testing import CliRunner

from skillmem import storage as S
from skillmem.cli import main as cli_main


def _seed(conn) -> None:
    S.upsert(conn, S.MemoryItem(slug="user-sergey", kind="user",
                                title="Owner profile", body="prefers short answers"))
    S.upsert(conn, S.MemoryItem(slug="feedback-no-guessing", kind="feedback",
                                title="Verify before claiming", body="search first"))
    S.upsert(conn, S.MemoryItem(slug="ref-something", kind="reference",
                                title="Some reference", body="not injected by default"))


def test_briefing_empty_db(conn):
    brief = S.briefing(conn)
    assert brief["sections"] == []
    assert brief["omitted"] == 0
    assert brief["approx_tokens"] == 0


def test_briefing_has_user_then_feedback_sections(conn):
    _seed(conn)
    brief = S.briefing(conn)  # default kinds: user, feedback
    kinds = [s["kind"] for s in brief["sections"]]
    assert kinds == ["user", "feedback"]  # user always first
    assert brief["sections"][0]["items"][0]["slug"] == "user-sergey"
    # reference is not part of the default briefing
    assert "reference" not in kinds


def test_briefing_respects_token_budget(conn):
    _seed(conn)
    brief = S.briefing(conn, budget_tokens=1)
    assert brief["sections"] == []
    assert brief["omitted"] == 2  # both default-kind rows skipped
    assert brief["approx_tokens"] == 0


def test_inject_cli_on_empty_db(memhome):
    result = CliRunner().invoke(cli_main, ["inject"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "# skillmem briefing" in result.output


def test_inject_cli_renders_sections(conn, memhome):
    _seed(conn)
    result = CliRunner().invoke(cli_main, ["inject"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "## USER" in result.output
    assert "## FEEDBACK" in result.output
    assert "[user-sergey] Owner profile" in result.output

    result = CliRunner().invoke(
        cli_main, ["inject", "--format", "json"], catch_exceptions=False
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert [s["kind"] for s in parsed["sections"]] == ["user", "feedback"]
