"""`skillmem hook *` — cross-platform Claude Code hooks.

Cyrillic fixtures are intentional: bilingual (RU+EN) search is a feature,
and these tests exercise the Cyrillic code path end to end.
"""

from __future__ import annotations

import json
import time
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


def test_tool_recall_reads_notebook_path(db: Path, tmp_path: Path):
    """NotebookEdit carries notebook_path, not file_path — reading only the
    latter left notebook edits with an empty query and no recall at all."""
    conn = S.connect(db)
    S.upsert(conn, S.MemoryItem(
        slug="skill-notebook-deploy", kind="skill",
        title="Прогон ноутбука analysis.ipynb перед деплоем",
        body="trigger: правки в analysis.ipynb; steps: прогнать все ячейки."))
    conn.commit()
    out = _hook(db, "tool-recall", {
        "session_id": "nb-1", "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": "/work/analysis.ipynb"},
    })
    assert "analysis.ipynb" in out


def test_dedup_ledger_lives_in_private_state_dir(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch):
    """In a shared /tmp a neighbour could pre-create the ledger and mute recall."""
    # XDG_STATE_HOME alone is ignored on Windows — the explicit override is the
    # only isolation that holds on every OS (that is why it exists).
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SKILLMEM_STATE_DIR", str(tmp_path / "state" / "skillmem"))
    from skillmem import hooks as H
    p = H._dedup_file("abc-123")
    assert str(tmp_path) in str(p) and p.name == "abc-123.txt"


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
        stdout=fake_summary.encode("utf-8"), returncode=0, stderr=b""))

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
            returncode=0, stderr=b"")

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
                               returncode=1, stderr=b"")

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
            returncode=0, stderr=b"")

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


def _fake_claude(monkeypatch, *, stdout=b"", rc=0, stderr=b"", raises=None):
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    calls = []

    def run(*a, **kw):
        calls.append(list(a[0]) if a else [])
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, returncode=rc, stderr=stderr)

    monkeypatch.setattr(H.subprocess, "run", run)
    return calls


def test_session_recap_never_overwrites_a_fresher_note(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A slow Stop and the SessionEnd behind it can overlap: whoever read less
    of the transcript must not land last."""
    proj, payload = _recap_fixture(tmp_path)
    memory = proj / "memory"
    memory.mkdir()
    note = memory / f"session-{__import__('datetime').date.today()}-abcd1234ffff.md"
    note.write_text(
        "---\nname: x\nmetadata:\n  type: note\n"
        "  transcript_bytes: 999999\n---\n\nFINAL\n", encoding="utf-8")
    _fake_claude(monkeypatch, stdout=b"## DONE\nOLD\n" + b"x" * 120, rc=0)
    _hook(db, "session-recap", payload)
    assert "FINAL" in note.read_text(encoding="utf-8")


def test_session_end_recap_runs_even_with_no_free_slot(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Dropping the final recap on a busy slot loses the closing turns for good."""
    proj, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H, "_acquire_recap_slot", lambda: None)
    calls = _fake_claude(monkeypatch, stdout=b"## DONE\n" + b"x" * 120, rc=0)
    _hook(db, "session-recap", payload)                      # Stop: skipped
    assert calls == []
    _hook(db, "session-recap", {**payload, "hook_event_name": "SessionEnd"})
    assert len(calls) == 1


def test_fallback_drops_hygiene_but_never_safety(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A summariser with tools is the hole this closes, so an unsupported safety
    flag means no recap at all. An unsupported hygiene flag is worth dropping."""
    _, payload = _recap_fixture(tmp_path)

    # 1. generic failure: no retry at all
    calls = _fake_claude(monkeypatch, rc=1, stderr=b"API error: 529 overloaded")
    _hook(db, "session-recap", payload)
    assert len(calls) == 1

    # 2. hygiene flag unknown: retry, still carrying the safety flags
    _, p2 = _recap_fixture(tmp_path / "second")
    p2["session_id"] = "bbbb2222-ffff-0000-1111-222233334444"
    calls2 = _fake_claude(
        monkeypatch, rc=1,
        stderr=b"error: unknown option '--no-session-persistence'")
    _hook(db, "session-recap", p2)
    assert len(calls2) == 2
    assert "--strict-mcp-config" in calls2[1] and "--tools" in calls2[1]
    assert "--no-session-persistence" not in calls2[1]

    # 3. safety flag unknown: give up, and say so
    proj3, p3 = _recap_fixture(tmp_path / "third")
    p3["session_id"] = "cccc3333-ffff-0000-1111-222233334444"
    calls3 = _fake_claude(
        monkeypatch, rc=1, stderr=b"error: unknown option '--strict-mcp-config'")
    _hook(db, "session-recap", p3)
    assert len(calls3) == 1
    assert list((proj3 / "memory").glob("session-*.md")) == []


def test_recap_timeout_is_not_retried(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    proj, payload = _recap_fixture(tmp_path)
    import subprocess as sp
    calls = _fake_claude(monkeypatch, raises=sp.TimeoutExpired("claude", 45))
    _hook(db, "session-recap", payload)
    assert len(calls) == 1
    assert list((proj / "memory").glob("session-*.md")) == []


def test_recap_indexes_its_note_into_the_db(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """SessionEnd registers only the recap, and hooks on one event run in
    parallel — the closing note must not wait for some later migrate."""
    monkeypatch.setenv("SKILLMEM_DB", str(db))
    _, payload = _recap_fixture(tmp_path)
    _fake_claude(monkeypatch, stdout="## DONE\nПочинили хук рекапа.\n".encode() + b"x" * 120, rc=0)
    _hook(db, "session-recap", {**payload, "hook_event_name": "SessionEnd"})
    conn = S.connect(db)
    rows = conn.execute(
        "select slug, source_session from memory_items where kind='note'").fetchall()
    assert any(r["slug"].startswith("session-") for r in rows)
    assert any(r["source_session"] == payload["session_id"] for r in rows)


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
        return SimpleNamespace(stdout=b"x" * 200, returncode=0, stderr=b"")

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


def test_manual_recap_gets_the_full_budget(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Asking by hand skips the rate limit but is not inside SessionEnd's 60s
    ceiling — the first live run of `skillmem recap` timed out at 45s because of
    exactly this conflation."""
    _, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    seen: list[int] = []

    def run(*a, **kw):
        seen.append(kw.get("timeout", 0))
        return SimpleNamespace(stdout=b"## DONE\n" + b"x" * 120, returncode=0, stderr=b"")

    monkeypatch.setattr(H.subprocess, "run", run)
    H.run_recap({**payload, "hook_event_name": "Manual", "force": True})
    assert seen and seen[0] > H.RECAP_TIMEOUT_FINAL

    seen.clear()
    H.run_recap({**payload, "hook_event_name": "SessionEnd"})
    assert seen and seen[0] <= H.RECAP_TIMEOUT_FINAL


def test_debounced_turn_does_not_read_the_transcript(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Stop fires per turn and a long session's transcript reaches tens of
    megabytes: counting its lines only to then skip costs more than the run."""
    proj, payload = _recap_fixture(tmp_path)
    _fake_claude(monkeypatch, stdout=b"## DONE\n" + b"x" * 120, rc=0)
    _hook(db, "session-recap", payload)            # first run: stamps the session

    opened: list[str] = []
    real_open = Path.open

    def spy(self, *a, **kw):
        opened.append(self.name)
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", spy)
    _hook(db, "session-recap", payload)            # debounced
    assert "sess.jsonl" not in opened


def test_publish_waits_for_the_lock_then_rechecks(tmp_path: Path):
    """The interleaving Astra reproduced: this run passed its freshness check,
    SessionEnd wrote the final recap meanwhile, and this run must stand down.
    The re-read happens inside the lock, so the late writer sees the final note.
    """
    import threading
    from skillmem import hooks as H
    note = tmp_path / "session-x.md"
    lock = note.with_name(note.name + ".publock")
    lock.write_text("held", encoding="utf-8")   # someone else is publishing

    def finish_first() -> None:
        time.sleep(0.15)
        note.write_text("---\nmetadata:\n  transcript_bytes: 999999\n---\n\nFINAL\n",
                        encoding="utf-8")
        lock.unlink()

    t = threading.Thread(target=finish_first)
    t.start()
    problem = H._publish_note(note, 100, "OLD\n")
    t.join()
    assert problem.startswith("skip:stale")
    assert "FINAL" in note.read_text(encoding="utf-8")


def test_session_end_recap_proceeds_over_a_held_session_lock(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A Stop recap in flight holds the per-session lock; the SessionEnd recap
    must wait briefly and then run anyway (publication is CAS-guarded), while a
    plain Stop under the same lock is skipped."""
    proj, payload = _recap_fixture(tmp_path)
    from skillmem import hooks as H
    monkeypatch.setattr(H.shutil, "which", lambda *_: "/fake/claude")
    monkeypatch.setattr(H.time, "sleep", lambda *_: None)
    calls = []

    def fake_run(*a, **kw):
        calls.append(1)
        return SimpleNamespace(stdout=("## DONE\n" + "x" * 150).encode(), returncode=0, stderr=b"")

    monkeypatch.setattr(H.subprocess, "run", fake_run)
    lock = H._recap_stamp(payload["session_id"]).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("held", encoding="utf-8")
    _hook(db, "session-recap", payload)                                  # Stop: skipped
    assert calls == []
    _hook(db, "session-recap", {**payload, "hook_event_name": "SessionEnd"})
    assert len(calls) == 1
    assert list((proj / "memory").glob("session-*.md"))
