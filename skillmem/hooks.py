"""Cross-platform Claude Code hooks: ``skillmem hook <name>``.

The same hook logic runs on macOS / Linux / Windows with no jq/sed/perl
dependencies. Each command reads the hook JSON from stdin and prints a
hookSpecificOutput JSON to stdout (or nothing — then Claude Code just
continues).

Events:
    SessionStart      -> mcp-guard, session-history
    UserPromptSubmit  -> verify-gate, auto-recall
    PreToolUse        -> tool-recall   (Bash|Edit|Write|NotebookEdit)
    Stop              -> session-recap (rate-limited; then `skillmem migrate`)
    SessionEnd        -> session-recap (once per session, not rate-limited)

recall/search are called directly through the storage layer (no child CLI
processes) — faster, and independent of PATH.
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

# Stopwords: high-frequency noise that drags in random matches ("claude" is the worst).
_STOPWORDS = re.compile(
    r"\b(claude|code|file|system|user|message|hook|prompt|tool|command)\b",
    re.IGNORECASE,
)
_SLUG_LINE = re.compile(r"^- \[([a-zа-я0-9-]+)\]", re.MULTILINE)

HOOK_LOG_MAX_BYTES = 2_000_000
HOOK_LOG_KEEP_LINES = 2000


def _utf8_stdio() -> None:
    """Windows consoles default to cp1251/cp866 — Claude Code expects UTF-8."""
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
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "skillmem"
    return Path.home() / ".local" / "state" / "skillmem"


def _hook_log_path() -> Path:
    override = os.environ.get("SKILLMEM_HOOK_LOG")
    return Path(override).expanduser() if override else _state_dir() / "hooks.log"


def _log_line(*fields: Any) -> None:
    """TSV hit-rate log with rotation (>2MB → keep the last 2000 lines)."""
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
        pass  # logging must never crash the hook


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
    """Shared composer for auto-recall / tool-recall: feedback + skills sections."""
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
        return ""  # a search failure must never crash the hook
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
    """Claude Code hooks (cross-platform, read hook JSON from stdin)."""
    _utf8_stdio()


# --------------------------------------------------------------------------- #
# UserPromptSubmit: auto-recall
# --------------------------------------------------------------------------- #

@hook_group.command("auto-recall")
@click.pass_context
def auto_recall(ctx: click.Context) -> None:
    """Top feedback+skills for the prompt text → additionalContext (~1500 chars)."""
    data = _read_input()
    prompt = str(data.get("prompt") or "")
    session_id = str(data.get("session_id") or "unknown")

    # A new user-prompt cycle starts → reset the dedup ledger.
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
        fb_header="### Relevant feedback:",
        skills_header="### Relevant skills (how this was done before):",
    )
    slugs = _extract_slugs(context)
    _append_seen(session_id, slugs)
    _log_line("auto-recall", session_id[:8], len(prompt), len(slugs),
              ",".join(slugs), len(context))
    if not context.strip():
        return
    if len(context) > 1500:
        context = context[:1500] + "…"
    _emit("UserPromptSubmit", f"📚 Auto-recall from memory (apply if relevant):\n\n{context}")


# --------------------------------------------------------------------------- #
# PreToolUse: tool-recall
# --------------------------------------------------------------------------- #

@hook_group.command("tool-recall")
@click.pass_context
def tool_recall(ctx: click.Context) -> None:
    """Skills/feedback matched on the tool input (Bash: command, Edit/Write: file_path)."""
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
        fb_header="### Rules/warnings from feedback:",
        skills_header="### Similar past tasks (skills):",
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
    _emit("PreToolUse", f"🔧 Tool-recall (context for {tool_name}):\n{context}")


# --------------------------------------------------------------------------- #
# SessionStart: mcp-guard
# --------------------------------------------------------------------------- #

@hook_group.command("mcp-guard")
def mcp_guard() -> None:
    """Compare mcpServers in ~/.claude.json against ~/.claude/mcp-baseline.txt."""
    _read_input()  # unused, but stdin must be drained
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
        f"⚠️ MCP guard: {len(actual)} of {len(expected)} expected servers connected. "
        f"MISSING: {' '.join(missing)}\n"
        "Tell the user about this in your very first reply. "
        "Restore with: claude mcp add-json <name> '<json>' -s user\n"
        f"Baseline: {base}"
    ))


# --------------------------------------------------------------------------- #
# SessionStart: session-history
# --------------------------------------------------------------------------- #

def _memory_dir_for(data: dict[str, Any]) -> Path | None:
    """The project's memory/ dir, derived from transcript_path (no hardcoded paths)."""
    tp = data.get("transcript_path")
    if tp:
        p = Path(str(tp)).expanduser()
        cand = p.parent / "memory"
        if cand.is_dir():
            return cand
    return None


@hook_group.command("session-history")
def session_history() -> None:
    """Top-3 freshest session-*.md from project memory → "where we left off" context."""
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
        # Strip yaml frontmatter (---...---) and blank lines.
        body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
        body = "\n".join(l for l in body.splitlines() if l.strip())[:600]
        if body:
            sections += f"\n\n### [{f.stem}]\n{body}…"
    if not sections:
        return
    if len(sections) > 2000:
        sections = sections[:2000] + "…"
    _emit("SessionStart", f"🧠 Recap of recent sessions (where we left off):{sections}")


# --------------------------------------------------------------------------- #
# UserPromptSubmit: verify-gate
# --------------------------------------------------------------------------- #

# Default trigger regex is deliberately bilingual (EN + RU): bilingual search
# is a product feature, and time-sensitive questions arrive in both languages.
# Override with SKILLMEM_VERIFY_PATTERN.
_VERIFY_DEFAULT = (
    "когда выйдет|вышел|вышла|вышло|выйдет|релиз|последняя версия|новая модель|"
    "Opus 4|Sonnet 4|Haiku 4|Claude 4\\.|сколько стоит|цена API|лимит|сейчас доступн|"
    "latest version|newest version|new model|latest model|just released|release date|"
    "when will .{0,20}(release|ship|launch)|how much does|api pricing|api price|"
    "rate limit|currently available"
)


@hook_group.command("verify-gate")
def verify_gate() -> None:
    """Inject a "search first" reminder when the prompt has time-sensitive triggers."""
    data = _read_input()
    prompt = str(data.get("prompt") or "")
    pattern = os.environ.get("SKILLMEM_VERIFY_PATTERN", _VERIFY_DEFAULT)
    try:
        if not re.search(pattern, prompt, re.IGNORECASE):
            return
    except re.error:
        return
    _emit("UserPromptSubmit", (
        "⚠️ VERIFY GATE: this prompt contains a time-sensitive trigger (model "
        "release / price / limit / availability). Call WebSearch (or another "
        "live source) and verify the fact BEFORE making any claim. The system "
        "prompt is a snapshot taken when the CLI was built, not ground truth."
    ))


# --------------------------------------------------------------------------- #
# Stop: session-recap
# --------------------------------------------------------------------------- #

RECAP_PROMPT = """Distill this development-session transcript into a structured recap. No filler, 1-3 lines per bullet. Markdown only, with exactly the headings below. Write the recap in the language predominantly used in the session (mirror the user's language).

## DECISIONS
Key decisions with a one-line rationale.

## DONE
Concrete changes: files, features, fixes, services deployed.

## UNFINISHED
TODOs, open questions, follow-ups for the next session.

## NEW RULES/PREFERENCES
Patterns and user preferences that surfaced (especially new ones).

## KEY ENTITIES
File names, services, agents, people, domains, dates — bullet list.

Session transcript:
---
"""

def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """A typo in the environment must not take the whole CLI down with it."""
    try:
        return max(lo, min(hi, int(os.environ[name])))
    except (KeyError, ValueError, TypeError):
        return default


RECAP_MODEL = os.environ.get("SKILLMEM_RECAP_MODEL", "claude-haiku-4-5-20251001")
RECAP_TIMEOUT = _env_int("SKILLMEM_RECAP_TIMEOUT", 85, 5, 600)
RECAP_MAX_TRANSCRIPT = 51_200  # last 50KB of filtered text
# Stop fires after every assistant turn, not when the session closes, so an
# ordinary working day would otherwise pay for a recap per turn and leave
# hundreds of near-identical notes behind. One note per session per day, and at
# most one model call per session per interval.
RECAP_MIN_INTERVAL = _env_int("SKILLMEM_RECAP_MIN_INTERVAL", 600, 0, 86_400)
# Second barrier, independent of the recursion guard: even if something spawns
# recaps in a storm, no more than this many run at once.
RECAP_MAX_PARALLEL = _env_int("SKILLMEM_RECAP_MAX_PARALLEL", 2, 1, 16)
# Flags the summariser does not need and that cost real money: MCP servers boot
# in every child session, and a persisted transcript leaves a ghost session
# behind. Dropped automatically on a CLI too old to know them.
RECAP_LEAN_FLAGS = ["--strict-mcp-config", "--no-session-persistence"]


def _recap_stamp(session_id: str) -> Path:
    """Marks the last recap ATTEMPT — a failed call must not reopen the budget."""
    safe = re.sub(r"[^A-Za-z0-9-]", "", session_id or "unknown")[:64] or "unknown"
    return _state_dir() / "recap-stamps" / f"{safe}.stamp"


def _acquire_recap_slot() -> Path | None:
    """Take one of RECAP_MAX_PARALLEL slots; None when all are busy.

    O_EXCL rather than fcntl — Windows has no fcntl. A lock older than two
    timeouts is reclaimed: whoever held it cannot still be alive, and a process
    killed with SIGKILL never runs its cleanup.
    """
    d = _state_dir() / "recap-slots"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    stale = RECAP_TIMEOUT * 2
    for i in range(RECAP_MAX_PARALLEL):
        lock = d / f"slot{i}.lock"
        try:
            if lock.exists() and time.time() - lock.stat().st_mtime > stale:
                lock.unlink()
        except OSError:
            pass
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except OSError:
            continue
    return None


def _filter_transcript(path: Path) -> str:
    """Keep only user/assistant text; skip tool_use/tool_result/thinking."""
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
    """Session recap via `claude -p` → session note in the project's memory/."""
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
    if line_count < 20:  # an accidentally opened session — nothing to recap
        return

    # SessionEnd is the session's last word — it must never be rate-limited away,
    # or the closing turns never reach memory.
    force = (str(data.get("hook_event_name") or "") == "SessionEnd"
             or os.environ.get("SKILLMEM_RECAP_FORCE") == "1")
    stamp = _recap_stamp(session_id)
    if not force:
        try:
            age = time.time() - stamp.stat().st_mtime
        except OSError:
            age = None
        if age is not None and 0 <= age < RECAP_MIN_INTERVAL:
            _log_line("session-recap", session_id[:8],
                      f"skip:debounce {int(age)}s < {RECAP_MIN_INTERVAL}s")
            return

    memory_dir = transcript.parent / "memory"
    try:
        memory_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log_line("session-recap", session_id[:8], f"skip:memory-dir {type(exc).__name__}")
        return

    # One note per session per day: a later recap rewrites the same file, so the
    # note stays current instead of the day filling up with partial copies.
    sid = session_id.replace("-", "")[:12]
    day = datetime.now().strftime("%Y-%m-%d")
    slug = f"session-{day}-{sid}"
    outfile = memory_dir / f"{slug}.md"

    filtered = _filter_transcript(transcript)
    if not filtered.strip():
        return
    payload = filtered.encode("utf-8")[-RECAP_MAX_TRANSCRIPT:].decode("utf-8", errors="replace")

    claude_bin = shutil.which("claude") or shutil.which("claude.cmd")
    if not claude_bin:
        _log_line("session-recap", session_id[:8], "skip:no-claude-cli")
        return
    slot = _acquire_recap_slot()
    if slot is None:
        _log_line("session-recap", session_id[:8], "skip:busy")
        return
    # Stamp the ATTEMPT, not the success: a model that keeps failing must not
    # get a fresh call on every turn.
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    def _run(flags: list[str]) -> tuple[str, int]:
        proc = subprocess.run(
            [claude_bin, "-p", "--model", RECAP_MODEL, *flags],
            input=(RECAP_PROMPT + payload + "\n---\n").encode("utf-8"),
            capture_output=True, timeout=RECAP_TIMEOUT,
            # the child is a Claude Code session too: without this its own Stop
            # hook recaps the recap, and every generation spawns the next one.
            env={**os.environ, "SKILLMEM_NO_RECAP": "1"},
        )
        return proc.stdout.decode("utf-8", errors="replace").strip(), proc.returncode

    summary, code = "", -1
    try:
        summary, code = _run(RECAP_LEAN_FLAGS)
        if code != 0 and not summary:
            # An older CLI rejects the lean flags outright — try the plain call.
            summary, code = _run([])
            if code == 0:
                _log_line("session-recap", session_id[:8], "note:lean-flags-unsupported")
    except Exception as exc:
        _log_line("session-recap", session_id[:8], f"error {type(exc).__name__}")
    finally:
        try:
            slot.unlink()
        except OSError:
            pass
    # A non-zero exit means the text is an error message, not a recap — writing
    # it would overwrite a good note with noise.
    if code != 0 or len(summary) < 100:
        _log_line("session-recap", session_id[:8],
                  f"empty/failed rc={code} len={len(summary)} transcript={len(payload)}b")
        return

    desc = next(
        (l.strip() for l in summary.splitlines()
         if l.strip() and not l.startswith("#") and not l.startswith("---")),
        f"Session recap {day}",
    )[:120]
    ended = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = outfile.with_name(outfile.name + f".tmp-{os.getpid()}")
    tmp.write_text(
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
    # Replace in one step: a crash mid-write must not leave a half note behind.
    os.replace(tmp, outfile)
    _log_line("session-recap", session_id[:8], f"wrote {outfile.name} ({len(summary)}b)")
