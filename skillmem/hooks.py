"""Cross-platform Claude Code hooks: ``skillmem hook <name>``.

Порт bash-хуков (~/.claude/hooks/*.sh) внутрь пакета, чтобы одна и та же
логика работала на macOS / Linux / Windows без jq/sed/perl. Каждая команда
читает JSON хука из stdin и печатает hookSpecificOutput JSON в stdout
(или ничего — тогда Claude Code просто продолжает).

Events:
    SessionStart      -> mcp-guard, session-history
    UserPromptSubmit  -> verify-gate, auto-recall
    PreToolUse        -> tool-recall   (Bash|Edit|Write|NotebookEdit)
    Stop              -> session-recap (потом `skillmem migrate` индексирует)

Отличие от bash-версии: recall/search зовутся напрямую через storage
(без запуска двух дочерних CLI-процессов) — быстрее и не зависит от PATH.

Fix vs bash: фильтр feedback `.rank < -5` был мёртвым с гибридного поиска
v0.6 (rank стал позицией 1..N, не BM25-скором) — секция feedback молча
не инжектилась. Здесь rank-порог убран: search() возвращает только матчи.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from . import storage as S

# --------------------------------------------------------------------------- #
# shared plumbing
# --------------------------------------------------------------------------- #

# Стоп-слова: высокочастотный шум, тянущий случайные матчи ("claude" — худший).
_STOPWORDS = re.compile(
    r"\b(claude|code|file|system|user|message|hook|prompt|tool|command)\b",
    re.IGNORECASE,
)
_SLUG_LINE = re.compile(r"^- \[([a-zа-я0-9-]+)\]", re.MULTILINE)

HOOK_LOG_MAX_BYTES = 2_000_000
HOOK_LOG_KEEP_LINES = 2000


def _utf8_stdio() -> None:
    """Windows-консоль по умолчанию cp1251/cp866 — Claude Code ждёт UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass


def _read_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _emit(event: str, context: str) -> None:
    print(json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
        ensure_ascii=False,
    ))


def _state_dir() -> Path:
    if sys.platform == "win32":
        from platformdirs import user_state_dir
        return Path(user_state_dir(S.APP_NAME))
    return Path.home() / ".local" / "state" / "skillmem"


def _hook_log_path() -> Path:
    override = os.environ.get("SKILLMEM_HOOK_LOG")
    return Path(override).expanduser() if override else _state_dir() / "hooks.log"


def _log_line(*fields: Any) -> None:
    """TSV hit-rate лог с ротацией (как в bash: >2MB → хвост 2000 строк)."""
    try:
        path = _hook_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > HOOK_LOG_MAX_BYTES:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            path.write_text(
                "\n".join(lines[-HOOK_LOG_KEEP_LINES:]) + "\n", encoding="utf-8"
            )
        stamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\t".join(str(f) for f in (stamp, *fields)) + "\n")
    except Exception:
        pass  # лог не должен ронять хук


def _dedup_file(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9-]", "", session_id or "unknown")[:64] or "unknown"
    return Path(tempfile.gettempdir()) / f"skillmem-injected-{safe}.txt"


def _read_seen(session_id: str) -> set[str]:
    try:
        return {
            s for s in _dedup_file(session_id).read_text(encoding="utf-8").splitlines()
            if s.strip()
        }
    except Exception:
        return set()


def _append_seen(session_id: str, slugs: list[str]) -> None:
    try:
        with _dedup_file(session_id).open("a", encoding="utf-8") as fh:
            for s in slugs:
                fh.write(s + "\n")
    except Exception:
        pass


def _clean_query(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", _STOPWORDS.sub("", text)).strip()
    return cleaned if len(cleaned) >= 5 else text


def _one_line(body: str, limit: int) -> str:
    return re.sub(r"\s+", " ", body or "").strip()[:limit]


def _extract_slugs(context: str) -> list[str]:
    return sorted(set(_SLUG_LINE.findall(context)))


def _connect(ctx: click.Context):
    db = (ctx.obj or {}).get("db_path") if ctx.obj else None
    conn = S.connect(Path(db).expanduser() if db else None)
    S.init_schema(conn)
    return conn


def _recall_sections(
    conn,
    query: str,
    seen: set[str],
    *,
    skills_limit: int,
    fb_limit: int,
    body_chars: int,
    fb_header: str,
    skills_header: str,
    min_strength: float = 0.0,
) -> str:
    """Общий композер auto-recall / tool-recall: feedback + skills секции."""
    try:
        fb = [
            r for r in S.search(conn, query, kind="feedback", limit=fb_limit)
            if r["slug"] not in seen
        ]
        skills = [
            r for r in S.recall_skills(conn, query, limit=skills_limit, auto_reinforce=False)
            if r["slug"] not in seen and r.get("strength", 0.0) >= min_strength
        ]
    except Exception:
        return ""  # хук никогда не должен падать из-за поиска
    parts: list[str] = []
    if fb:
        parts.append(fb_header + "\n" + "\n".join(
            f"- [{r['slug']}] {r['title']}\n  {_one_line(r.get('body', ''), body_chars)}"
            for r in fb
        ))
    if skills:
        parts.append(skills_header + "\n" + "\n".join(
            f"- [{r['slug']}] {r['title']}\n  {_one_line(r.get('body', ''), body_chars)}"
            for r in skills
        ))
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# click group
# --------------------------------------------------------------------------- #

@click.group(name="hook")
def hook_group() -> None:
    """Claude Code hooks (кроссплатформенные, читают JSON из stdin)."""
    _utf8_stdio()


# --------------------------------------------------------------------------- #
# UserPromptSubmit: auto-recall
# --------------------------------------------------------------------------- #

@hook_group.command("auto-recall")
@click.pass_context
def auto_recall(ctx: click.Context) -> None:
    """Top feedback+skills по тексту промпта → additionalContext (~1500 chars)."""
    data = _read_input()
    prompt = str(data.get("prompt") or "")
    session_id = str(data.get("session_id") or "unknown")

    # Начало нового user-prompt цикла → обнулить dedup-леджер.
    try:
        _dedup_file(session_id).write_text("", encoding="utf-8")
    except Exception:
        pass

    if len(prompt) < 10:
        return
    query = _clean_query(prompt)
    if len(query) < 10:
        query = prompt

    try:
        conn = _connect(ctx)
    except Exception:
        return
    context = _recall_sections(
        conn, query, seen=set(),
        skills_limit=2, fb_limit=3, body_chars=400,
        fb_header="### Релевантные feedback:",
        skills_header="### Релевантные skills (как делали раньше):",
    )
    slugs = _extract_slugs(context)
    _append_seen(session_id, slugs)
    _log_line("auto-recall", session_id[:8], len(prompt), len(slugs),
              ",".join(slugs), len(context))
    if not context.strip():
        return
    if len(context) > 1500:
        context = context[:1500] + "…"
    _emit("UserPromptSubmit", f"📚 Auto-recall из памяти (применяй если релевантно):\n\n{context}")


# --------------------------------------------------------------------------- #
# PreToolUse: tool-recall
# --------------------------------------------------------------------------- #

@hook_group.command("tool-recall")
@click.pass_context
def tool_recall(ctx: click.Context) -> None:
    """Скиллы/feedback по инпуту тула (Bash: command, Edit/Write: file_path)."""
    data = _read_input()
    tool_name = str(data.get("tool_name") or "")
    session_id = str(data.get("session_id") or "unknown")
    tool_input = data.get("tool_input") or {}

    if tool_name == "Bash":
        query = str(tool_input.get("command") or "")[:200]
    elif tool_name in ("Edit", "Write", "NotebookEdit"):
        query = str(tool_input.get("file_path") or "")[:200]
    else:
        return
    if len(query) < 5:
        return
    query = _clean_query(query)

    try:
        conn = _connect(ctx)
    except Exception:
        return
    seen = _read_seen(session_id)
    context = _recall_sections(
        conn, query, seen=seen,
        skills_limit=2, fb_limit=2, body_chars=250,
        fb_header="### Правила/warnings из feedback:",
        skills_header="### Похожие задачи (skills):",
        min_strength=0.3,
    )
    slugs = _extract_slugs(context)
    _append_seen(session_id, slugs)
    _log_line("tool-recall", session_id[:8], tool_name, len(slugs),
              ",".join(slugs), len(context))
    if not context.strip():
        return
    if len(context) > 1000:
        context = context[:1000] + "…"
    _emit("PreToolUse", f"🔧 Tool-recall (контекст для {tool_name}):\n{context}")


# --------------------------------------------------------------------------- #
# SessionStart: mcp-guard
# --------------------------------------------------------------------------- #

@hook_group.command("mcp-guard")
def mcp_guard() -> None:
    """Сверить mcpServers в ~/.claude.json с эталоном ~/.claude/mcp-baseline.txt."""
    _read_input()  # не используем, но stdin надо дочитать
    conf = Path.home() / ".claude.json"
    base = Path.home() / ".claude" / "mcp-baseline.txt"
    if not conf.exists() or not base.exists():
        return
    try:
        actual = set((json.loads(conf.read_text(encoding="utf-8")).get("mcpServers") or {}).keys())
    except Exception:
        return
    expected = {
        line.strip() for line in base.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = sorted(expected - actual)
    if not missing:
        return
    _emit("SessionStart", (
        f"⚠️ MCP-щит: подключено {len(actual)} из {len(expected)} серверов. "
        f"НЕ ХВАТАЕТ: {' '.join(missing)}\n"
        "Скажи об этом пользователю в первом же ответе. "
        "Вернуть: claude mcp add-json <имя> '<json>' -s user\n"
        f"Эталон: {base}"
    ))


# --------------------------------------------------------------------------- #
# SessionStart: session-history
# --------------------------------------------------------------------------- #

def _memory_dir_for(data: dict[str, Any]) -> Path | None:
    """Каталог memory/ проекта — от transcript_path (не хардкодим -Users-...)."""
    tp = data.get("transcript_path")
    if tp:
        p = Path(str(tp)).expanduser()
        cand = p.parent / "memory"
        if cand.is_dir():
            return cand
    return None


@hook_group.command("session-history")
def session_history() -> None:
    """Top-3 свежих session-*.md из памяти проекта → контекст «откуда пришли»."""
    data = _read_input()
    memory_dir = _memory_dir_for(data)
    if memory_dir is None:
        return
    files = sorted(
        memory_dir.glob("session-*.md"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )[:3]
    sections = ""
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        # Срезать yaml frontmatter (---...---) и пустые строки.
        body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
        body = "\n".join(l for l in body.splitlines() if l.strip())[:600]
        if body:
            sections += f"\n\n### [{f.stem}]\n{body}…"
    if not sections:
        return
    if len(sections) > 2000:
        sections = sections[:2000] + "…"
    _emit("SessionStart", f"🧠 Recap последних сессий (контекст откуда пришли):{sections}")


# --------------------------------------------------------------------------- #
# UserPromptSubmit: verify-gate
# --------------------------------------------------------------------------- #

_VERIFY_DEFAULT = (
    "когда выйдет|вышел|вышла|вышло|выйдет|релиз|последняя версия|новая модель|"
    "Opus 4|Sonnet 4|Haiku 4|Claude 4\\.|сколько стоит|цена API|лимит|сейчас доступн"
)


@hook_group.command("verify-gate")
def verify_gate() -> None:
    """Напоминание «сначала WebSearch» на time-sensitive триггеры в промпте."""
    data = _read_input()
    prompt = str(data.get("prompt") or "")
    pattern = os.environ.get("SKILLMEM_VERIFY_PATTERN", _VERIFY_DEFAULT)
    try:
        if not re.search(pattern, prompt, re.IGNORECASE):
            return
    except re.error:
        return
    _emit("UserPromptSubmit", (
        "⚠️ ГЕЙТ: вопрос содержит time-sensitive триггер (релиз модели / цена / "
        "лимит / новая модель). Сначала вызови WebSearch/поиск и проверь факт ДО "
        "любого утверждения. Системный промпт — снимок на момент сборки CLI, "
        "не ground truth."
    ))


# --------------------------------------------------------------------------- #
# Stop: session-recap
# --------------------------------------------------------------------------- #

RECAP_PROMPT = """Выжми из транскрипта сессии разработки структурированную выжимку. Без воды, каждый пункт 1-3 строки. Только маркдаун с заголовками ниже.

## ЧТО РЕШИЛИ
Ключевые решения с кратким обоснованием.

## ЧТО СДЕЛАЛИ
Конкретные изменения: файлы, фичи, фиксы, развёрнутые сервисы.

## ЧТО НЕДОДЕЛАЛИ
TODO, открытые вопросы, follow-ups для следующей сессии.

## НОВЫЕ ПРАВИЛА/ПРЕДПОЧТЕНИЯ
Паттерны и preferences от пользователя которые всплыли (особенно если новые).

## КЛЮЧЕВЫЕ СУЩНОСТИ
Имена файлов, сервисов, агентов, людей, доменов, дат — bullet list.

Транскрипт сессии:
---
"""

RECAP_MODEL = os.environ.get("SKILLMEM_RECAP_MODEL", "claude-haiku-4-5-20251001")
RECAP_TIMEOUT = int(os.environ.get("SKILLMEM_RECAP_TIMEOUT", "85"))
RECAP_MAX_TRANSCRIPT = 51_200  # последние 50KB отфильтрованного текста


def _filter_transcript(path: Path) -> str:
    """Только текст user/assistant; tool_use/tool_result/thinking — мимо."""
    out: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") not in ("user", "assistant"):
                continue
            content = (rec.get("message") or {}).get("content") or []
            if not isinstance(content, list):
                continue
            text = " ".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )
            if len(text) > 10:
                out.append(f"{rec['type']}: {text}")
    return "\n".join(out)


@hook_group.command("session-recap")
def session_recap() -> None:
    """Выжимка сессии через `claude -p` → session-note в memory/ проекта."""
    if os.environ.get("SKILLMEM_NO_RECAP") == "1":
        return
    data = _read_input()
    session_id = str(data.get("session_id") or "")
    tp = data.get("transcript_path")
    if not session_id or not tp:
        return
    transcript = Path(str(tp)).expanduser()
    if not transcript.is_file():
        return
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
    except Exception:
        return
    if line_count < 20:  # случайно открытая сессия — не выжимаем
        return

    memory_dir = transcript.parent / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    filtered = _filter_transcript(transcript)
    if not filtered.strip():
        return
    payload = filtered.encode("utf-8")[-RECAP_MAX_TRANSCRIPT:].decode("utf-8", errors="replace")

    claude_bin = shutil.which("claude") or shutil.which("claude.cmd")
    if not claude_bin:
        _log_line("session-recap", session_id[:8], "skip:no-claude-cli")
        return
    try:
        proc = subprocess.run(
            [claude_bin, "-p", "--model", RECAP_MODEL],
            input=(RECAP_PROMPT + payload + "\n---\n").encode("utf-8"),
            capture_output=True, timeout=RECAP_TIMEOUT,
        )
        summary = proc.stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        summary = ""
    if len(summary) < 100:
        _log_line("session-recap", session_id[:8],
                  f"empty/failed len={len(summary)} transcript={len(payload)}b")
        return

    sid8 = session_id.replace("-", "")[:8]
    ts = datetime.now().strftime("%Y-%m-%d-%H%M")
    slug = f"session-{ts}-{sid8}"
    desc = next(
        (l.strip() for l in summary.splitlines()
         if l.strip() and not l.startswith("#") and not l.startswith("---")),
        f"Session recap {ts}",
    )[:120]
    ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    outfile = memory_dir / f"{slug}.md"
    outfile.write_text(
        "---\n"
        f"name: {slug}\n"
        f"description: {json.dumps(desc, ensure_ascii=False)}\n"
        "metadata:\n"
        "  type: note\n"
        f"  source_session: {session_id}\n"
        f"  ended_at: {ended}\n"
        "---\n\n"
        f"{summary}\n",
        encoding="utf-8",
    )
    _log_line("session-recap", session_id[:8], f"wrote {outfile.name} ({len(summary)}b)")
