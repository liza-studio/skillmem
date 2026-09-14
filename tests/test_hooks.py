"""`skillmem hook *` — cross-platform Claude Code hooks.

Cyrillic fixtures are intentional: bilingual (RU+EN) search is a feature,
and these tests exercise the Cyrillic code path end to end.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from skillmem import storage as S
from skillmem.cli import main as cli_main


def _hook(db: Path, name: str, payload: dict, env: dict | None = None) -> str:
    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["--db", str(db), "hook", name],
        input=json.dumps(payload, ensure_ascii=False),
        env=env or {}, catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    return result.output


@pytest.fixture
def db(memhome: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("SKILLMEM_HOOK_LOG", str(tmp_path / "hooks.log"))
    path = memhome / "memory.db"
    conn = S.connect(path)
    S.init_schema(conn)
    S.upsert(conn, S.MemoryItem(
        slug="feedback-deploy-rules", kind="feedback",
        title="Деплой только через deploy_gated.sh",
        body="Никаких хотфиксов напрямую на прод. Только гейт."))
    S.upsert(conn, S.MemoryItem(
        slug="skill-restart-bot", kind="skill",
        title="Рестарт бота без потери ответов",
        body="Только scripts/restart_bot.sh — дренаж in-flight."))
    conn.execute("UPDATE memory_items SET strength=0.9 WHERE slug='skill-restart-bot'")
    conn.commit()
    conn.close()
    return path


def test_auto_recall_injects_feedback_and_skills(db: Path):
    out = _hook(db, "auto-recall", {
        "prompt": "как правильно задеплоить хотфикс и рестартовать бота на проде",
        "session_id": "sess-hooks-test-1",
    })
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    # The feedback section must be present alongside skills.
    assert "feedback-deploy-rules" in ctx
    assert "skill-restart-bot" in ctx


def test_auto_recall_silent_on_short_prompt(db: Path):
    out = _hook(db, "auto-recall", {"prompt": "hi", "session_id": "s2"})
    assert out.strip() == ""


def test_tool_recall_dedups_against_auto_recall(db: Path):
    sid = "sess-hooks-dedup"
    _hook(db, "auto-recall", {
        "prompt": "как задеплоить и рестартовать бота на проде без потери ответов",
        "session_id": sid,
    })
    out = _hook(db, "tool-recall", {
        "tool_name": "Bash", "session_id": sid,
        "tool_input": {"command": "bash scripts/restart_bot.sh деплой прод"},
    })
    # Slugs already injected by auto-recall in this cycle are not repeated.
    assert "skill-restart-bot" not in out
    assert "feedback-deploy-rules" not in out


def test_tool_recall_ignores_unknown_tools(db: Path):
    out = _hook(db, "tool-recall", {
        "tool_name": "WebSearch", "session_id": "s3",
        "tool_input": {"query": "деплой бота"},
    })
    assert out.strip() == ""


def test_verify_gate_triggers_and_stays_silent(db: Path):
    # RU trigger (default pattern is bilingual RU+EN)
    hot = _hook(db, "verify-gate", {"prompt": "какая последняя версия Claude?"})
    assert "VERIFY GATE" in hot
    # EN trigger
    hot_en = _hook(db, "verify-gate", {"prompt": "what is the latest version of Claude?"})
    assert "VERIFY GATE" in hot_en
    cold = _hook(db, "verify-gate", {"prompt": "поправь отступы в файле"})
    assert cold.strip() == ""


def test_mcp_guard_reports_missing_servers(db: Path, fakehome: Path):
    (fakehome / ".claude.json").write_text(
        json.dumps({"mcpServers": {"skillmem": {}}}), encoding="utf-8")
    (fakehome / ".claude").mkdir(exist_ok=True)
    (fakehome / ".claude" / "mcp-baseline.txt").write_text(
        "# baseline\nskillmem\nplaywright\nfirecrawl\n", encoding="utf-8")
    out = _hook(db, "mcp-guard", {})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "firecrawl" in ctx and "playwright" in ctx
    assert "skillmem" not in ctx.split("MISSING:")[1].splitlines()[0].split()


def test_mcp_guard_silent_when_baseline_matches(db: Path, fakehome: Path):
    (fakehome / ".claude.json").write_text(
        json.dumps({"mcpServers": {"skillmem": {}}}), encoding="utf-8")
    (fakehome / ".claude").mkdir(exist_ok=True)
    (fakehome / ".claude" / "mcp-baseline.txt").write_text("skillmem\n", encoding="utf-8")
    out = _hook(db, "mcp-guard", {})
    assert out.strip() == ""


def test_session_history_reads_memory_next_to_transcript(db: Path, tmp_path: Path):
    proj = tmp_path / "projects" / "-Users-someone"
    mem = proj / "memory"
    mem.mkdir(parents=True)
    (mem / "session-2026-08-01-1200-aaaa.md").write_text(
        "---\nname: x\ndescription: \"y\"\n---\n\n## ЧТО СДЕЛАЛИ\nПеренесли хуки\n",
        encoding="utf-8")
    transcript = proj / "abc.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    out = _hook(db, "session-history", {"transcript_path": str(transcript)})
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Перенесли хуки" in ctx
    assert "session-2026-08-01-1200-aaaa" in ctx


def test_session_recap_writes_note(db: Path, tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch):
    proj = tmp_path / "projects" / "-Users-someone"
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": f"вопрос номер {i} про деплой и хуки"}]}})
        for i in range(25)
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    fake_summary = "## ЧТО РЕШИЛИ\nПеренесли skillmem на Windows.\n" + "x" * 120
    monkeypatch.setattr(H.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout=fake_summary.encode("utf-8"), returncode=0))

    out = _hook(db, "session-recap", {
        "session_id": "abcd1234-ffff-0000-1111-222233334444",
        "transcript_path": str(transcript),
    })
    assert out.strip() == ""  # recap injects nothing, it only writes a file
    notes = list((proj / "memory").glob("session-*abcd1234ffff.md"))
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert "Перенесли skillmem на Windows" in text
    assert "source_session: abcd1234-ffff-0000-1111-222233334444" in text


def test_session_recap_debounces_and_rewrites_one_note(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Stop fires per turn: the second recap within the interval is skipped, and
    a later one rewrites the same note instead of adding another."""
    proj = tmp_path / "projects" / "-Users-someone"
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    transcript.write_text("\n".join(
        json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": f"вопрос номер {i} про деплой и хуки"}]}})
        for i in range(25)) + "\n", encoding="utf-8")

    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        return SimpleNamespace(
            stdout=(f"## DONE\nпрогон номер {len(calls)}\n" + "x" * 120).encode(),
            returncode=0)

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    payload = {"session_id": "abcd1234-ffff-0000-1111-222233334444",
               "transcript_path": str(transcript)}

    _hook(db, "session-recap", payload)
    _hook(db, "session-recap", payload)          # same turn-ish: debounced
    assert len(calls) == 1
    notes = list((proj / "memory").glob("session-*.md"))
    assert len(notes) == 1
    assert "прогон номер 1" in notes[0].read_text(encoding="utf-8")

    # past the interval: the model runs again and the one note is rewritten
    monkeypatch.setattr(H, "RECAP_MIN_INTERVAL", 0)
    _hook(db, "session-recap", payload)
    assert len(calls) == 2
    notes = list((proj / "memory").glob("session-*.md"))
    assert len(notes) == 1
    assert "прогон номер 2" in notes[0].read_text(encoding="utf-8")


def _recap_fixture(tmp_path: Path):
    proj = tmp_path / "projects" / "-Users-someone"
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    transcript.write_text("\n".join(
        json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": f"вопрос номер {i} про деплой и хуки"}]}})
        for i in range(25)) + "\n", encoding="utf-8")
    return proj, {"session_id": "abcd1234-ffff-0000-1111-222233334444",
                  "transcript_path": str(transcript)}


def test_session_recap_rejects_failed_run_and_still_debounces(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A non-zero exit is an error message, not a recap — and the attempt still
    counts, or a permanently failing model buys a call on every single turn."""
    proj, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        return SimpleNamespace(stdout=b"Error: usage limit reached. " * 20,
                               returncode=1)

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    _hook(db, "session-recap", payload)
    assert list((proj / "memory").glob("session-*.md")) == []
    _hook(db, "session-recap", payload)
    assert len(calls) == 1, "failed attempt must be stamped, not retried at once"


def test_session_recap_session_end_ignores_debounce(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """SessionEnd is the session's last word: rate-limiting it loses the tail."""
    proj, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        return SimpleNamespace(
            stdout=(f"## DONE\nпрогон {len(calls)}\n" + "x" * 120).encode(),
            returncode=0)

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    _hook(db, "session-recap", payload)
    _hook(db, "session-recap", {**payload, "hook_event_name": "SessionEnd"})
    assert len(calls) == 2
    notes = list((proj / "memory").glob("session-*.md"))
    assert len(notes) == 1 and "прогон 2" in notes[0].read_text(encoding="utf-8")


def test_session_recap_skips_when_all_slots_busy(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Second barrier after the recursion guard: no storm of parallel children."""
    proj, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    monkeypatch.setattr(H, "_acquire_recap_slot", lambda: None)
    called = []
    monkeypatch.setattr(H.subprocess, "run",
                        lambda *a, **kw: called.append(1))
    _hook(db, "session-recap", payload)
    assert called == [] and list((proj / "memory").glob("session-*.md")) == []


def test_env_int_survives_garbage_and_clamps(monkeypatch: pytest.MonkeyPatch):
    """A typo in the environment must not take the whole CLI down with it."""
    from skillmem import hooks as H
    monkeypatch.setenv("SKILLMEM_TEST_INT", "oops")
    assert H._env_int("SKILLMEM_TEST_INT", 600, 0, 86_400) == 600
    monkeypatch.setenv("SKILLMEM_TEST_INT", "-5")
    assert H._env_int("SKILLMEM_TEST_INT", 600, 0, 86_400) == 0
    monkeypatch.setenv("SKILLMEM_TEST_INT", "999999")
    assert H._env_int("SKILLMEM_TEST_INT", 600, 0, 86_400) == 86_400


def test_session_recap_optout_reaches_the_child(db: Path, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch):
    """The spawned `claude -p` is a session too: without the flag its own Stop
    hook recaps the recap, and every generation spawns the next one."""
    proj = tmp_path / "projects" / "-Users-someone"
    proj.mkdir(parents=True)
    transcript = proj / "sess.jsonl"
    transcript.write_text("\n".join(
        json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": f"вопрос номер {i} про деплой и хуки"}]}})
        for i in range(25)) + "\n", encoding="utf-8")

    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    seen: dict = {}

    def fake_run(*a, **kw):
        seen.update(kw)
        return SimpleNamespace(stdout=b"x" * 200, returncode=0)

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    _hook(db, "session-recap", {
        "session_id": "abcd1234-ffff-0000-1111-222233334444",
        "transcript_path": str(transcript),
    })
    assert seen["env"]["SKILLMEM_NO_RECAP"] == "1"


def test_session_recap_respects_optout(db: Path, tmp_path: Path):
    out = _hook(db, "session-recap",
                {"session_id": "x", "transcript_path": str(tmp_path / "no.jsonl")},
                env={"SKILLMEM_NO_RECAP": "1"})
    assert out.strip() == ""


def test_state_dir_respects_xdg_state_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    from skillmem import hooks as H
    monkeypatch.setattr(H.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert H._state_dir() == tmp_path / "xdg-state" / "skillmem"
    # fallback without the env var: ~/.local/state/skillmem.
    # Path.home() reads USERPROFILE on a real Windows host, so set both —
    # mocking sys.platform alone does not change home-dir resolution.
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert H._state_dir() == tmp_path / ".local" / "state" / "skillmem"
