"""SQLite + FTS5 storage layer for skillmem.

Schema is the MVP version from Liza_Mind_Concept §2.4. Pillar #1
(birth certificate / supersession / death record) is built in:
- created_at/updated_at and source_session = birth certificate
- ttl_days + freshness_until = expiration
- supersedes_id chain + memory_history table = death record

FTS5 mirrors title+body via triggers so writes stay simple.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from platformdirs import user_data_dir


log = logging.getLogger("skillmem.storage")


# --------------------------------------------------------------------------- #
# paths & connection
# --------------------------------------------------------------------------- #

APP_NAME = "skillmem"

# Compiled regexes used across stemming, search, and conflict detection.
import re as _re  # noqa: E402 — needed early for module-level patterns

_CYRILLIC = _re.compile(r"[А-Яа-яЁё]")
_WORD_RE = _re.compile(r"[\wа-яё]+", _re.IGNORECASE | _re.UNICODE)
_PRIVATE_BLOCK = _re.compile(r"<private>.*?</private>", _re.DOTALL)
_API_KEY = _re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{20,}|ghp_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_\-]{30,})\b"
)
# Extended secret patterns (Phase 2). High-precision — низкий риск ложных срабатываний.
_PEM_KEY = _re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    _re.DOTALL,
)
_AWS_KEY = _re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_TG_BOT_TOKEN = _re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{32,}\b")
_JWT = _re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
# password=... / "token": "..." / secret: ... — режем только значение, ключ оставляем.
_SECRET_ASSIGN = _re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|api[_\-]?key|token|access[_\-]?token)\b"""
    r"""(\s*[:=]\s*)(["']?)([^\s"',;]{6,})(["']?)""",
)
_WIKILINK = _re.compile(r"\[\[([^\]\n]+?)\]\]")


def default_data_dir() -> Path:
    override = os.environ.get("SKILLMEM_HOME")
    if override:
        return Path(override).expanduser()
    return Path(user_data_dir(APP_NAME, appauthor=False))


def default_db_path() -> Path:
    """``SKILLMEM_DB`` wins over ``SKILLMEM_HOME`` wins over OS user-data."""
    db_override = os.environ.get("SKILLMEM_DB")
    if db_override:
        return Path(db_override).expanduser()
    return default_data_dir() / "memory.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection in autocommit mode.

    Multi-statement writes (upsert + history + body file) must be wrapped
    in :func:`tx` so they commit atomically. Anything else just runs in
    autocommit — keeps callers simple, no need to remember `.commit()`.
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # A fleet of agents writes through this DB concurrently. Without a busy
    # timeout, a writer that meets a held lock fails instantly with
    # "database is locked" instead of waiting the fraction of a second the
    # other transaction needs. Override with SKILLMEM_BUSY_TIMEOUT_MS.
    busy_ms = _env_int("SKILLMEM_BUSY_TIMEOUT_MS", 10_000)
    conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
    return conn


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


import uuid as _uuid


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic write block. Re-entrant via SAVEPOINTs so nested ``with tx``
    works (outer ``upsert`` inside a batch import loop, etc.)."""
    if conn.in_transaction:
        sp = "sm_sp_" + _uuid.uuid4().hex[:12]  # guaranteed unique name
        conn.execute(f"SAVEPOINT {sp}")
        try:
            yield conn
        except Exception:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            raise
        else:
            conn.execute(f"RELEASE {sp}")
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# schema
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'note',
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    body_path       TEXT,                          -- non-NULL = body on disk (long docs)
    project         TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    topics          TEXT NOT NULL DEFAULT '[]',
    visibility      TEXT NOT NULL DEFAULT 'private',
    agent           TEXT,
    source_session  TEXT,
    attachments     TEXT NOT NULL DEFAULT '[]',
    ttl_days        INTEGER,
    freshness_until INTEGER,
    wordcount       INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT NOT NULL,
    supersedes_id   INTEGER REFERENCES memory_items(id) ON DELETE SET NULL,
    confidence      REAL NOT NULL DEFAULT 1.0,
    strength        REAL NOT NULL DEFAULT 1.0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    deleted_at      INTEGER,
    embedding       BLOB                           -- float32×384, semantic recall (v6)
);

CREATE INDEX IF NOT EXISTS idx_memory_kind     ON memory_items(kind);
CREATE INDEX IF NOT EXISTS idx_memory_project  ON memory_items(project);
CREATE INDEX IF NOT EXISTS idx_memory_updated  ON memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_hash     ON memory_items(content_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(
    title, body, tags, topics, slug UNINDEXED,
    content='memory_items', content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memory_items_ai AFTER INSERT ON memory_items BEGIN
    INSERT INTO mem_fts(rowid, title, body, tags, topics, slug)
    VALUES (new.id, new.title, new.body, new.tags, new.topics, new.slug);
END;

CREATE TRIGGER IF NOT EXISTS memory_items_ad AFTER DELETE ON memory_items BEGIN
    INSERT INTO mem_fts(mem_fts, rowid, title, body, tags, topics, slug)
    VALUES('delete', old.id, old.title, old.body, old.tags, old.topics, old.slug);
END;

CREATE TRIGGER IF NOT EXISTS memory_items_au AFTER UPDATE ON memory_items BEGIN
    INSERT INTO mem_fts(mem_fts, rowid, title, body, tags, topics, slug)
    VALUES('delete', old.id, old.title, old.body, old.tags, old.topics, old.slug);
    INSERT INTO mem_fts(rowid, title, body, tags, topics, slug)
    VALUES (new.id, new.title, new.body, new.tags, new.topics, new.slug);
END;

CREATE TABLE IF NOT EXISTS mem_links (
    from_slug TEXT NOT NULL,
    to_slug   TEXT NOT NULL,
    PRIMARY KEY (from_slug, to_slug)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON mem_links(to_slug);

CREATE TABLE IF NOT EXISTS memory_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL,
    old_title    TEXT,
    old_body     TEXT NOT NULL,
    changed_at   INTEGER NOT NULL,
    changed_by   TEXT,
    reason       TEXT,
    prev_hash    TEXT,                    -- SHA256 of previous row, NULL for genesis
    self_hash    TEXT                     -- SHA256 chained over this row + prev_hash
);
CREATE INDEX IF NOT EXISTS idx_history_slug ON memory_history(slug);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Phase 4 groundwork: recall traces. One row per recall event records which
-- skills were surfaced for which query. The raw signal a future prompt
-- optimizer (DSPy/GEPA) needs: what got recalled, how often, and (later)
-- whether it helped. Idempotent CREATE — no schema-version bump needed.
CREATE TABLE IF NOT EXISTS skill_traces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id  TEXT,
    agent       TEXT,
    query       TEXT,
    recalled    TEXT NOT NULL DEFAULT '[]',   -- JSON [{slug, score}]
    helped      INTEGER,                       -- NULL until a feedback signal sets it
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_created ON skill_traces(created_at);
"""


CURRENT_SCHEMA_VERSION = 7


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema bootstrap. Cheap on every call after first run."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION),),
    )


def _current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if not row:
        return 0
    try:
        return int(row["value"])
    except (ValueError, TypeError):
        return 0


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only schema patches. Backfills run once, then are skipped."""
    # Self-healing guard: ensure the embedding column exists regardless of the
    # version gate below. A migration interrupted between bumping the version
    # and running its ALTER (e.g. a concurrent recall hook) could otherwise
    # strand the DB at v6 with no column. Cheap idempotent check on every open.
    live_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "embedding" not in live_cols:
        conn.execute("ALTER TABLE memory_items ADD COLUMN embedding BLOB")
    # v7: skill lifecycle state (active -> stale -> archived). Self-healing,
    # like embedding, so the column is guaranteed regardless of the version gate.
    if "lifecycle" not in live_cols:
        conn.execute(
            "ALTER TABLE memory_items ADD COLUMN lifecycle TEXT NOT NULL DEFAULT 'active'"
        )

    if _current_schema_version(conn) >= CURRENT_SCHEMA_VERSION:
        return

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "body_path" not in cols:
        conn.execute("ALTER TABLE memory_items ADD COLUMN body_path TEXT")
    if "stemmed" not in cols:
        conn.execute(
            "ALTER TABLE memory_items ADD COLUMN stemmed TEXT NOT NULL DEFAULT ''"
        )

    history_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_history)")}
    history_needs_chain = "self_hash" not in history_cols
    if history_needs_chain:
        conn.execute("ALTER TABLE memory_history ADD COLUMN prev_hash TEXT")
        conn.execute("ALTER TABLE memory_history ADD COLUMN self_hash TEXT")

    existing_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )}
    if "mem_fts_stem" not in existing_tables:
        conn.executescript("""
            CREATE VIRTUAL TABLE mem_fts_stem USING fts5(
                stemmed,
                content='memory_items', content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER mem_stem_ai AFTER INSERT ON memory_items BEGIN
                INSERT INTO mem_fts_stem(rowid, stemmed) VALUES (new.id, new.stemmed);
            END;
            CREATE TRIGGER mem_stem_ad AFTER DELETE ON memory_items BEGIN
                INSERT INTO mem_fts_stem(mem_fts_stem, rowid, stemmed)
                VALUES('delete', old.id, old.stemmed);
            END;
            CREATE TRIGGER mem_stem_au AFTER UPDATE ON memory_items BEGIN
                INSERT INTO mem_fts_stem(mem_fts_stem, rowid, stemmed)
                VALUES('delete', old.id, old.stemmed);
                INSERT INTO mem_fts_stem(rowid, stemmed) VALUES (new.id, new.stemmed);
            END;
        """)

    rows = conn.execute(
        "SELECT id, title, body FROM memory_items WHERE stemmed IS NULL OR stemmed = ''"
    ).fetchall()
    if rows:
        log.info("backfilling Snowball stems for %d rows", len(rows))
        for r in rows:
            stemmed = _stem_text(f"{r['title']}\n{r['body']}")
            conn.execute(
                "UPDATE memory_items SET stemmed = ? WHERE id = ?",
                (stemmed, r["id"]),
            )

    if history_needs_chain:
        _backfill_history_chain(conn)

    # --- v5: skill learning columns ---
    if "access_count" not in cols:
        conn.execute(
            "ALTER TABLE memory_items ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
        )
    if "last_accessed_at" not in cols:
        conn.execute("ALTER TABLE memory_items ADD COLUMN last_accessed_at INTEGER")

    # --- v6: semantic embedding column ---
    # Column is added empty; backfill is a separate, optional, network-bound
    # step (`skillmem reindex-embeddings`) so the migration never blocks on a
    # model download. Recall falls back to BM25 for rows without an embedding.
    if "embedding" not in cols:
        conn.execute("ALTER TABLE memory_items ADD COLUMN embedding BLOB")

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION),),
    )


def _chain_hash(prev_hash: str | None, payload: dict) -> str:
    """Deterministic SHA256 over ``prev_hash || canonical JSON of payload``.

    JSON is sorted-keys + ensure_ascii=False, and the resulting string is
    Unicode-normalised to NFC before hashing so the same logical record
    produces the same hash on systems with different normalization (macOS
    HFS+ likes NFD, most Linux uses NFC)."""
    import unicodedata
    body = unicodedata.normalize(
        "NFC",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    h = hashlib.sha256()
    h.update((prev_hash or "").encode("utf-8"))
    h.update(b"\n")
    h.update(body.encode("utf-8"))
    return h.hexdigest()


def _backfill_history_chain(conn: sqlite3.Connection) -> None:
    """Build the hash chain over every existing history row in chronological order."""
    rows = conn.execute(
        "SELECT id, slug, old_title, old_body, changed_at, changed_by, reason "
        "FROM memory_history ORDER BY changed_at, id"
    ).fetchall()
    if not rows:
        return
    log.info("backfilling memory_history hash-chain for %d rows", len(rows))
    prev = None
    for r in rows:
        payload = {
            "slug": r["slug"], "old_title": r["old_title"], "old_body": r["old_body"],
            "changed_at": r["changed_at"], "changed_by": r["changed_by"],
            "reason": r["reason"],
        }
        h = _chain_hash(prev, payload)
        conn.execute(
            "UPDATE memory_history SET prev_hash = ?, self_hash = ? WHERE id = ?",
            (prev, h, r["id"]),
        )
        prev = h


def _last_chain_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT self_hash FROM memory_history "
        "WHERE self_hash IS NOT NULL ORDER BY changed_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return row["self_hash"] if row else None


# --------------------------------------------------------------------------- #
# Snowball stemming preprocessor
# --------------------------------------------------------------------------- #

try:
    import snowballstemmer as _snowball
    _STEM_RU = _snowball.stemmer("russian")
    _STEM_EN = _snowball.stemmer("english")
    _STEM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _STEM_RU = None
    _STEM_EN = None
    _STEM_AVAILABLE = False


def _stem_word(word: str) -> str:
    if not _STEM_AVAILABLE:
        return word.lower()
    lower = word.lower()
    is_ru = _CYRILLIC.search(lower) is not None
    return (_STEM_RU if is_ru else _STEM_EN).stemWord(lower)


def _stem_text(text: str) -> str:
    """Return text where every token is replaced by its Snowball stem.

    Tokens shorter than 3 chars are dropped (BM25 noise). Stems are joined by
    spaces; punctuation is discarded — Snowball is what gives semantic recall
    here, FTS5 just BM25-ranks the result.
    """
    if not text:
        return ""
    out: list[str] = []
    for match in _WORD_RE.finditer(text):
        w = match.group(0)
        if len(w) < 3:
            continue
        out.append(_stem_word(w))
    return " ".join(out)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _now() -> int:
    return int(time.time())


def _hash(title: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(title.encode("utf-8"))
    h.update(b"\n\x00\n")
    h.update(body.encode("utf-8"))
    return h.hexdigest()


def _wordcount(body: str) -> int:
    return len(body.split())


def _json_list(values: Iterable[str] | None) -> str:
    if not values:
        return "[]"
    return json.dumps(list(values), ensure_ascii=False)


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return list(v) if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


# --------------------------------------------------------------------------- #
# domain model
# --------------------------------------------------------------------------- #


@dataclass
class MemoryItem:
    slug: str
    kind: str = "note"
    title: str = ""
    body: str = ""
    body_path: str | None = None
    project: str | None = None
    tags: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    visibility: str = "private"
    agent: str | None = None
    source_session: str | None = None
    attachments: list[str] = field(default_factory=list)
    ttl_days: int | None = None
    freshness_until: int | None = None
    confidence: float = 1.0
    strength: float = 1.0
    access_count: int = 0
    last_accessed_at: int | None = None
    id: int | None = None
    supersedes_id: int | None = None
    content_hash: str = ""
    wordcount: int = 0
    created_at: int = 0
    updated_at: int = 0
    deleted_at: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryItem":
        return cls(
            id=row["id"],
            slug=row["slug"],
            kind=row["kind"],
            title=row["title"],
            body=row["body"],
            body_path=row["body_path"] if "body_path" in row.keys() else None,
            project=row["project"],
            tags=_parse_json_list(row["tags"]),
            topics=_parse_json_list(row["topics"]),
            visibility=row["visibility"],
            agent=row["agent"],
            source_session=row["source_session"],
            attachments=_parse_json_list(row["attachments"]),
            ttl_days=row["ttl_days"],
            freshness_until=row["freshness_until"],
            wordcount=row["wordcount"],
            content_hash=row["content_hash"],
            supersedes_id=row["supersedes_id"],
            confidence=row["confidence"],
            strength=row["strength"],
            access_count=row["access_count"] if "access_count" in row.keys() else 0,
            last_accessed_at=row["last_accessed_at"] if "last_accessed_at" in row.keys() else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# privacy filter (stub — full version in Phase 2)
# --------------------------------------------------------------------------- #


def scrub(text: str) -> str:
    text = _PRIVATE_BLOCK.sub("[private redacted]", text)
    text = _PEM_KEY.sub("[private-key redacted]", text)
    text = _API_KEY.sub("[api-key redacted]", text)
    text = _AWS_KEY.sub("[aws-key redacted]", text)
    text = _TG_BOT_TOKEN.sub("[tg-token redacted]", text)
    text = _JWT.sub("[jwt redacted]", text)
    # сохраняем имя ключа и разделитель, маскируем только значение
    text = _SECRET_ASSIGN.sub(r"\1\2\3[secret redacted]\5", text)
    return text


# --------------------------------------------------------------------------- #
# write / update
# --------------------------------------------------------------------------- #


class MemoryConflict(Exception):
    """Raised when writing a slug that already exists without a reason."""


# --------------------------------------------------------------------------- #
# external body storage for long-form docs
# --------------------------------------------------------------------------- #

DOC_BODY_THRESHOLD = 8 * 1024  # 8 KB inline cap; longer bodies go on disk
DOC_EXCERPT_CHARS = 4 * 1024   # what we keep inside the DB for FTS


def docs_dir() -> Path:
    path = default_data_dir() / "docs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _should_externalize(item: "MemoryItem") -> bool:
    return item.kind == "document" or len(item.body) > DOC_BODY_THRESHOLD


def _make_excerpt(body: str, limit: int = DOC_EXCERPT_CHARS) -> str:
    """Return a body excerpt that ends on a paragraph/sentence/word boundary."""
    if len(body) <= limit:
        return body
    head = body[:limit]
    for cut in ("\n\n", "\n", ". ", " "):
        idx = head.rfind(cut)
        if idx > limit // 2:
            return head[: idx + len(cut)].rstrip() + "\n\n…"
    return head + "…"


def _body_filename(slug: str) -> str:
    """Deterministic filename: ``<safe-slug>__<hash8>.md`` — collision-proof."""
    safe = _re.sub(r"[^\w.\-]+", "-", slug, flags=_re.UNICODE).strip("-") or "untitled"
    h = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    return f"{safe}__{h}.md"


def _write_body_file(slug: str, body: str) -> str:
    """Atomic write: temp file in the same dir, then ``os.replace``."""
    dest = docs_dir() / _body_filename(slug)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, dest)
    return dest.name  # relative to docs_dir()


def _stage_body_file(slug: str, body: str) -> tuple[Path, Path]:
    """Write the new body to a scratch file WITHOUT publishing it yet.

    The write itself was already atomic, but it happened inside the DB
    transaction: if a later statement failed, SQLite rolled back to the old row
    while the file on disk already held the new text — excerpt, content_hash
    and body file silently disagreeing. Staging here and publishing after
    COMMIT keeps the two in step.
    """
    dest = docs_dir() / _body_filename(slug)
    tmp = dest.with_suffix(dest.suffix + f".staged-{_uuid.uuid4().hex[:8]}")
    tmp.write_text(body, encoding="utf-8")
    return tmp, dest


def _publish_body_file(staged: tuple[Path, Path]) -> str:
    tmp, dest = staged
    os.replace(tmp, dest)
    return dest.name


def _discard_body_file(staged: tuple[Path, Path] | None) -> None:
    if staged is None:
        return
    try:
        staged[0].unlink(missing_ok=True)
    except OSError:
        pass  # orphan scratch file is harmless; never mask the real error


def load_body(item: "MemoryItem") -> str:
    """Return the full body, materialising from disk when externalized."""
    if not item.body_path:
        return item.body
    path = docs_dir() / item.body_path
    if not path.exists():
        # Falling back to the excerpt keeps reads working, but silently serving
        # a truncated body as if it were the whole record is exactly the kind of
        # quiet data loss that goes unnoticed for months. Say so.
        log.warning(
            "body file missing for '%s' (%s) — returning the stored excerpt only",
            item.slug, path,
        )
        return item.body
    return path.read_text(encoding="utf-8")


def upsert(
    conn: sqlite3.Connection,
    item: MemoryItem,
    *,
    reason: str | None = None,
    force: bool = False,
    check_conflicts: bool = False,
    links: Iterable[str] | None = None,
) -> MemoryItem:
    item.title = scrub(item.title)
    item.body = scrub(item.body)
    item.wordcount = _wordcount(item.body)
    item.content_hash = _hash(item.title, item.body)
    now = _now()

    # Decide externalization (no I/O yet). The actual file write happens inside
    # the transaction so a SQL failure rolls everything back.
    full_body = item.body
    externalize = _should_externalize(item)
    if externalize:
        item.body_path = _body_filename(item.slug)
        item.body = _make_excerpt(full_body)
    else:
        item.body_path = None

    stemmed = _stem_text(
        f"{item.title}\n{full_body}\n" + " ".join(item.tags + item.topics)
    )

    existing = conn.execute(
        "SELECT * FROM memory_items WHERE slug = ?", (item.slug,)
    ).fetchone()

    if existing is None:
        # Conflict check runs BEFORE the transaction so we hold no write lock
        # while we're scanning FTS5 — keeps concurrent searchers responsive.
        if check_conflicts and not force:
            conflicts = find_conflicts(conn, item.title, item.body)
            if conflicts:
                raise MemoryConflict(
                    "duplicate-candidates:" + json.dumps(conflicts, ensure_ascii=False)
                )
        if item.ttl_days and not item.freshness_until:
            item.freshness_until = now + item.ttl_days * 86400
        if not item.created_at:
            item.created_at = now
        item.updated_at = now

        staged = _stage_body_file(item.slug, full_body) if externalize else None
        try:
            with tx(conn):
                cur = conn.execute(
                    """
                    INSERT INTO memory_items (
                        slug, kind, title, body, body_path, stemmed, project, tags,
                        topics, visibility, agent, source_session, attachments, ttl_days,
                        freshness_until, wordcount, content_hash, supersedes_id,
                        confidence, strength, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item.slug, item.kind, item.title, item.body, item.body_path,
                        stemmed, item.project,
                        _json_list(item.tags), _json_list(item.topics), item.visibility,
                        item.agent, item.source_session, _json_list(item.attachments),
                        item.ttl_days, item.freshness_until,
                        item.wordcount, item.content_hash, item.supersedes_id,
                        item.confidence, item.strength,
                        item.created_at, item.updated_at,
                    ),
                )
                item.id = cur.lastrowid
                if links is not None:
                    _replace_links_inner(conn, item.slug, links)
        except sqlite3.IntegrityError as exc:
            _discard_body_file(staged)
            # Another writer inserted this slug between our existence check and
            # this INSERT. That is the same situation the pre-check reports as a
            # conflict, so callers should see the same exception — not a raw
            # driver error they have no reason to special-case.
            if "memory_items.slug" in str(exc) or "UNIQUE" in str(exc).upper():
                raise MemoryConflict(
                    f"slug '{item.slug}' was created concurrently by another writer"
                ) from exc
            raise
        except BaseException:
            _discard_body_file(staged)
            raise
        if staged is not None:
            _publish_body_file(staged)
        _set_embedding(conn, item.id, item.title, full_body)
        return item

    if existing["content_hash"] == item.content_hash:
        return MemoryItem.from_row(existing)

    if not reason and not force:
        raise MemoryConflict(
            f"slug '{item.slug}' exists; pass reason='...' or force=True to overwrite"
        )

    old_path = existing["body_path"] if "body_path" in existing.keys() else None
    old_body = existing["body"]
    if old_path:
        old_full = docs_dir() / old_path
        if old_full.exists():
            try:
                old_body = old_full.read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("could not read old body for history: %s", exc)

    freshness = item.freshness_until
    if item.ttl_days and not freshness:
        freshness = now + item.ttl_days * 86400

    # Staged, not published: if the transaction below rolls back, the old body
    # file must survive untouched (see _stage_body_file).
    staged = _stage_body_file(item.slug, full_body) if externalize else None
    try:
        _upsert_update_tx(
            conn, item=item, existing=existing, now=now, reason=reason,
            stemmed=stemmed, freshness=freshness, old_body=old_body, links=links,
        )
    except BaseException:
        _discard_body_file(staged)
        raise

    if staged is not None:
        _publish_body_file(staged)

    # Clean up the previous body file *after* the commit succeeded — failure
    # to delete just leaves an orphan, not a corrupted record.
    if old_path and old_path != item.body_path:
        old_file = docs_dir() / old_path
        if old_file.exists():
            try:
                old_file.unlink()
            except OSError as exc:
                log.warning("orphan body file remains at %s: %s", old_file, exc)

    item.id = existing["id"]
    item.created_at = existing["created_at"]
    item.updated_at = now
    item.freshness_until = freshness
    _set_embedding(conn, item.id, item.title, full_body)
    return item


def _upsert_update_tx(
    conn: sqlite3.Connection, *, item: "MemoryItem", existing: Any, now: int,
    reason: str | None, stemmed: str, freshness: int | None, old_body: str,
    links: list[str] | None,
) -> None:
    with tx(conn):
        prev_hash = _last_chain_hash(conn)
        history_payload = {
            "slug": existing["slug"], "old_title": existing["title"],
            "old_body": old_body, "changed_at": now,
            "changed_by": item.agent, "reason": reason or "force overwrite",
        }
        self_hash = _chain_hash(prev_hash, history_payload)
        conn.execute(
            """
            INSERT INTO memory_history (
                slug, old_title, old_body, changed_at, changed_by, reason,
                prev_hash, self_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                existing["slug"], existing["title"], old_body,
                now, item.agent, reason or "force overwrite",
                prev_hash, self_hash,
            ),
        )
        conn.execute(
            """
            UPDATE memory_items SET
                kind = ?, title = ?, body = ?, body_path = ?, stemmed = ?, project = ?,
                tags = ?, topics = ?, visibility = ?, agent = ?, source_session = ?,
                attachments = ?, ttl_days = ?, freshness_until = ?, wordcount = ?,
                content_hash = ?, supersedes_id = ?, confidence = ?, strength = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                item.kind, item.title, item.body, item.body_path, stemmed, item.project,
                _json_list(item.tags), _json_list(item.topics),
                item.visibility, item.agent, item.source_session,
                _json_list(item.attachments),
                item.ttl_days, freshness, item.wordcount, item.content_hash,
                item.supersedes_id, item.confidence, item.strength, now,
                existing["id"],
            ),
        )
        if links is not None:
            _replace_links_inner(conn, item.slug, links)


def _set_embedding(conn: sqlite3.Connection, item_id: int | None, title: str, body: str) -> None:
    """Best-effort embedding write — runs OUTSIDE the main write tx.

    Kept off the hot insert/update path so a slow/cold model load never holds
    the write lock, and a missing/broken embedder never blocks a memory write.
    A row left without an embedding simply falls back to BM25 at recall time;
    ``reindex-embeddings`` can backfill it later.
    """
    if item_id is None:
        return
    from . import embed as _embed

    if not _embed.semantic_enabled():
        return
    blob = _embed.embed_text(_embed.doc_text(title, body))
    if blob is None:
        return
    # No explicit commit: in autocommit mode (isolation_level=None) the bare
    # execute persists immediately; if upsert was called inside an outer `with
    # tx`, this UPDATE simply joins that transaction. Committing here would
    # close the caller's transaction early and break SAVEPOINT nesting.
    try:
        conn.execute(
            "UPDATE memory_items SET embedding = ? WHERE id = ?", (blob, item_id)
        )
    except sqlite3.Error as exc:
        log.warning("could not store embedding for id=%s: %s", item_id, exc)


def reindex_embeddings(
    conn: sqlite3.Connection, *, only_missing: bool = True
) -> dict[str, int]:
    """Backfill semantic embeddings for stored items. One-time / maintenance.

    Reads body from disk for externalized rows so long docs get embedded too.
    Returns counts. No-op (skipped=all) when the embedder is unavailable.
    """
    from . import embed as _embed

    if not _embed.available():
        return {"updated": 0, "skipped": 0, "unavailable": 1}
    where = "WHERE deleted_at IS NULL"
    if only_missing:
        where += " AND embedding IS NULL"
    rows = conn.execute(f"SELECT * FROM memory_items {where}").fetchall()
    updated = 0
    for row in rows:
        item = MemoryItem.from_row(row)
        body = load_body(item) if row["body_path"] else row["body"]
        blob = _embed.embed_text(_embed.doc_text(row["title"], body))
        if blob is None:
            continue
        conn.execute(
            "UPDATE memory_items SET embedding = ? WHERE id = ?", (blob, row["id"])
        )
        updated += 1
    conn.commit()
    return {"updated": updated, "total": len(rows)}


def soft_delete(conn: sqlite3.Connection, slug: str, reason: str) -> bool:
    row = conn.execute(
        "SELECT * FROM memory_items WHERE slug = ?", (slug,)
    ).fetchone()
    if not row:
        return False
    now = _now()
    with tx(conn):
        prev_hash = _last_chain_hash(conn)
        payload = {
            "slug": row["slug"], "old_title": row["title"], "old_body": row["body"],
            "changed_at": now, "changed_by": None, "reason": f"deleted: {reason}",
        }
        self_hash = _chain_hash(prev_hash, payload)
        conn.execute(
            "INSERT INTO memory_history (slug, old_title, old_body, changed_at, reason,"
            " prev_hash, self_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["slug"], row["title"], row["body"], now,
             f"deleted: {reason}", prev_hash, self_hash),
        )
        conn.execute(
            "UPDATE memory_items SET deleted_at = ? WHERE id = ?", (now, row["id"]),
        )
    return True


# --------------------------------------------------------------------------- #
# chain verification (borrowed from NOM §6 tamper-evidence)
# --------------------------------------------------------------------------- #


@dataclass
class ChainBreak:
    row_id: int
    slug: str
    changed_at: int
    expected_prev: str | None
    actual_prev: str | None
    expected_self: str
    actual_self: str | None


def verify_history(conn: sqlite3.Connection) -> tuple[int, list[ChainBreak]]:
    """Walk memory_history in chronological order, recomputing SHA256 chain.

    Returns ``(rows_checked, breaks)``. Breaks are NOT raised — caller decides.

    Important: once a row is detected as broken we keep walking with the
    **observed** ``self_hash`` (whatever the row claims), not the recomputed
    one. That way every downstream row whose ``prev_hash`` doesn't match the
    *actual* previous self_hash is also flagged — tampering propagates and is
    visible, instead of silently healing after the first edit.
    """
    rows = conn.execute(
        "SELECT id, slug, old_title, old_body, changed_at, changed_by, reason, "
        "prev_hash, self_hash FROM memory_history ORDER BY changed_at, id"
    ).fetchall()
    prev_actual = None  # the hash we'll require the next row's prev_hash to equal
    breaks: list[ChainBreak] = []
    for r in rows:
        payload = {
            "slug": r["slug"], "old_title": r["old_title"], "old_body": r["old_body"],
            "changed_at": r["changed_at"], "changed_by": r["changed_by"],
            "reason": r["reason"],
        }
        expected_self = _chain_hash(prev_actual, payload)
        if r["prev_hash"] != prev_actual or r["self_hash"] != expected_self:
            breaks.append(ChainBreak(
                row_id=r["id"], slug=r["slug"], changed_at=r["changed_at"],
                expected_prev=prev_actual, actual_prev=r["prev_hash"],
                expected_self=expected_self, actual_self=r["self_hash"],
            ))
        # Continue with whatever the row CLAIMS — propagating tampering will
        # surface as cascading breaks instead of silently healing.
        prev_actual = r["self_hash"]
    return len(rows), breaks


# --------------------------------------------------------------------------- #
# wikilinks
# --------------------------------------------------------------------------- #

def extract_wikilinks(body: str) -> list[str]:
    out: list[str] = []
    for match in _WIKILINK.finditer(body):
        target = match.group(1).split("|", 1)[0].strip()
        if target:
            out.append(target)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _replace_links_inner(
    conn: sqlite3.Connection, from_slug: str, to_slugs: Iterable[str]
) -> None:
    """No-transaction variant — caller guarantees we're inside ``tx()``."""
    conn.execute("DELETE FROM mem_links WHERE from_slug = ?", (from_slug,))
    rows = [(from_slug, t) for t in dict.fromkeys(to_slugs)]
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO mem_links (from_slug, to_slug) VALUES (?, ?)",
            rows,
        )


def replace_links(
    conn: sqlite3.Connection, from_slug: str, to_slugs: Iterable[str]
) -> None:
    """Public helper — wraps :func:`_replace_links_inner` in a transaction."""
    with tx(conn):
        _replace_links_inner(conn, from_slug, to_slugs)


# --------------------------------------------------------------------------- #
# read / search
# --------------------------------------------------------------------------- #


def get(conn: sqlite3.Connection, slug: str) -> MemoryItem | None:
    row = conn.execute(
        "SELECT * FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    return MemoryItem.from_row(row) if row else None


def history(conn: sqlite3.Connection, slug: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM memory_history WHERE slug = ? ORDER BY changed_at DESC",
        (slug,),
    ).fetchall()
    return [dict(r) for r in rows]


def links_from(conn: sqlite3.Connection, slug: str) -> list[str]:
    rows = conn.execute(
        "SELECT to_slug FROM mem_links WHERE from_slug = ? ORDER BY to_slug",
        (slug,),
    ).fetchall()
    return [r["to_slug"] for r in rows]


def links_to(conn: sqlite3.Connection, slug: str) -> list[str]:
    rows = conn.execute(
        "SELECT from_slug FROM mem_links WHERE to_slug = ? ORDER BY from_slug",
        (slug,),
    ).fetchall()
    return [r["from_slug"] for r in rows]


def list_items(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 50,
    recent: bool = True,
) -> list[MemoryItem]:
    where = ["deleted_at IS NULL"]
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if project:
        where.append("project = ?")
        params.append(project)
    order = "updated_at DESC" if recent else "slug ASC"
    sql = f"SELECT * FROM memory_items WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [MemoryItem.from_row(r) for r in rows]


def _escape_fts(query: str) -> str:
    """Escape a user query for FTS5 MATCH against ``mem_fts_stem``.

    Tokens are Snowball-stemmed then quoted as prefix matches. We use OR
    semantics across tokens (standard search-engine behavior) and rely on
    BM25 to rank documents that match more of them higher. Implicit AND
    would be too strict for natural-language queries — a 5-word question
    almost never has all 5 stems in a single short message.
    """
    tokens = [t for t in query.split() if t]
    if not tokens:
        return '""'
    parts: list[str] = []
    for raw in tokens:
        stem = _stem_word(raw)
        if len(stem) < 2:
            continue
        safe = stem.replace('"', '""')
        parts.append(f'"{safe}"*')
    return " OR ".join(parts) if parts else '""'


def _escape_fts_or(tokens: Iterable[str]) -> str:
    """Stem-aware OR query (used by conflict detection)."""
    parts: list[str] = []
    for raw in tokens:
        stem = _stem_word(raw)
        if len(stem) < 2:
            continue
        safe = stem.replace('"', '""')
        parts.append(f'"{safe}"*')
    return " OR ".join(parts) if parts else '""'


SEARCH_BM25 = """
SELECT m.*, bm25(mem_fts_stem) AS rank
FROM mem_fts_stem
JOIN memory_items m ON m.id = mem_fts_stem.rowid
WHERE mem_fts_stem MATCH ?
  AND m.deleted_at IS NULL
  {kind_clause}
  {project_clause}
ORDER BY rank
LIMIT ?
"""


def _snippet_for(body: str, query: str, *, around: int = 90) -> str:
    """Render an excerpt around the first matched stem (cheap, language-aware)."""
    if not body:
        return ""
    stems = [_stem_word(t) for t in query.split() if len(_stem_word(t)) >= 2]
    if not stems:
        return body[: around * 2] + ("…" if len(body) > around * 2 else "")
    text = body
    lower = body.lower()
    idx = -1
    for stem in stems:
        # locate any word that starts with this stem (case-insensitive)
        for match in _WORD_RE.finditer(text):
            if _stem_word(match.group(0)).startswith(stem):
                idx = match.start()
                break
        if idx >= 0:
            break
    if idx < 0:
        return body[: around * 2] + ("…" if len(body) > around * 2 else "")
    start = max(0, idx - around)
    end = min(len(text), idx + around)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return pre + text[start:end].replace("\n", " ") + post


def _freshness(now: int, updated_at: int, freshness_until: int | None) -> tuple[str, int]:
    """Return (label, stale_days). 'fresh' when no TTL or still within it."""
    if not freshness_until:
        return "fresh", 0
    if now <= freshness_until:
        return "fresh", 0
    return "stale", (now - freshness_until) // 86400


# --------------------------------------------------------------------------- #
# hybrid retrieval: BM25 (lexical) + vector (semantic) fused via RRF
# --------------------------------------------------------------------------- #

RRF_K = 60          # standard Reciprocal Rank Fusion constant
_CANDIDATE_POOL = 50  # how many candidates each signal contributes before fusion
# Below this cosine a vector "match" is just the nearest unrelated item, so we
# drop it — keeps recall empty for genuinely-irrelevant queries (no context
# noise in the auto-recall hook). Measured 2026-06-09: irrelevant pairs scored
# <0.07, real cross-lingual matches >0.45 — a wide, stable margin around 0.25.
_MIN_COSINE = 0.25
# Strength tiebreaker for skill recall. A grid sweep over a RU/EN cross-lingual
# bench (2026-06-09) showed symmetric RRF (no vector over-weight) + a *gentle*
# strength bonus is optimal — 9/10 hit@1. Larger coefficients (the old 0.3) let
# high-strength skills outrank more relevant ones, dropping accuracy. Keep small.
SKILL_STRENGTH_COEF = 0.05


def _bm25_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    pool: int = _CANDIDATE_POOL,
) -> list[int]:
    """Lexical candidate ids, best-first, from the stemmed FTS5 index."""
    kind_clause = "AND m.kind = ?" if kind else ""
    project_clause = "AND m.project = ?" if project else ""
    sql = f"""
        SELECT m.id AS id, bm25(mem_fts_stem) AS r
        FROM mem_fts_stem
        JOIN memory_items m ON m.id = mem_fts_stem.rowid
        WHERE mem_fts_stem MATCH ?
          AND m.deleted_at IS NULL
          AND m.lifecycle != 'archived'
          {kind_clause}
          {project_clause}
        ORDER BY r
        LIMIT ?
    """
    params: list[Any] = [_escape_fts(query)]
    if kind:
        params.append(kind)
    if project:
        params.append(project)
    params.append(pool)
    return [row["id"] for row in conn.execute(sql, params).fetchall()]


def _vector_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    pool: int = _CANDIDATE_POOL,
) -> list[int]:
    """Semantic candidate ids, best-first, via brute-force cosine.

    At our scale (~10^3 rows) a full numpy matmul is sub-millisecond, so no
    vector index is needed. Returns [] when the embedder is unavailable, which
    makes the caller degrade to pure BM25.
    """
    from . import embed as _embed

    qb = _embed.pack_query(query)
    if qb is None:
        return []
    try:
        import numpy as np
    except Exception:
        return []
    kind_clause = "AND kind = ?" if kind else ""
    project_clause = "AND project = ?" if project else ""
    sql = (
        "SELECT id, embedding FROM memory_items "
        "WHERE embedding IS NOT NULL AND deleted_at IS NULL "
        "AND lifecycle != 'archived' "
        f"{kind_clause} {project_clause}"
    )
    params: list[Any] = []
    if kind:
        params.append(kind)
    if project:
        params.append(project)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    q = np.frombuffer(qb, dtype="float32")
    ids = [r["id"] for r in rows]
    mat = np.stack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])
    sims = mat @ q                       # both sides pre-normalized -> cosine
    order = np.argsort(-sims)[:pool]
    return [ids[int(i)] for i in order if float(sims[int(i)]) >= _MIN_COSINE]


def _rrf_scores(*ranked_lists: list[int]) -> dict[int, float]:
    """Reciprocal Rank Fusion: score = Σ 1/(K + rank). Scale-free, no tuning."""
    scores: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, id_ in enumerate(lst):
            scores[id_] = scores.get(id_, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def hybrid_rank_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 10,
) -> list[int]:
    """Fused id ranking. Falls back to pure BM25 when no vector signal."""
    bm = _bm25_ids(conn, query, kind=kind, project=project)
    vec = _vector_ids(conn, query, kind=kind, project=project)
    if not vec:
        return bm[:limit]
    scores = _rrf_scores(bm, vec)
    ordered = sorted(scores, key=lambda i: -scores[i])
    return ordered[:limit]


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    ids = hybrid_rank_ids(conn, query, kind=kind, project=project, limit=limit)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM memory_items WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    rows = [by_id[i] for i in ids if i in by_id]  # preserve fused order

    now = _now()
    out: list[dict[str, Any]] = []
    for pos, row in enumerate(rows, start=1):
        d = dict(row)
        d["tags"] = _parse_json_list(d.get("tags"))
        d["topics"] = _parse_json_list(d.get("topics"))
        d["attachments"] = _parse_json_list(d.get("attachments"))
        label, stale = _freshness(now, d["updated_at"], d.get("freshness_until"))
        d["freshness"] = label
        d["stale_days"] = stale
        d["snippet"] = _snippet_for(d.get("body", ""), query)
        # fused (RRF) position; the old BM25 column is gone in the hybrid path
        d["rank"] = pos
        out.append(d)
    return out


# --------------------------------------------------------------------------- #
# conflict detection (Jaccard overlap on body words)
# --------------------------------------------------------------------------- #

def _word_bag(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "") if len(w) >= 3}


def find_conflicts(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    *,
    threshold: float = 0.7,
    candidates: int = 5,
    exclude_slug: str | None = None,
) -> list[dict[str, Any]]:
    """Return existing memories whose word content overlaps the new one.

    Uses FTS5 BM25 to surface candidates (cheap), then computes asymmetric
    inclusion overlap ``|A ∩ B| / min(|A|, |B|)`` on the bag of words. This
    is what LML §5.2 means by "70% перекрытие": if 70% of one doc's words
    are in the other, treat it as a duplicate — regardless of length.
    No LLM involved.
    """
    bag = _word_bag(title + "\n" + body)
    if len(bag) < 5:
        return []  # too short to make a meaningful overlap claim

    # Conflict detection needs OR semantics across the bag; use stem-OR query
    # against mem_fts_stem (same index used by /search).
    fts_query = _escape_fts_or(list(bag)[:32])
    if fts_query == '""':
        return []
    try:
        rows = conn.execute(
            "SELECT m.id, m.slug, m.title, m.body FROM mem_fts_stem "
            "JOIN memory_items m ON m.id = mem_fts_stem.rowid "
            "WHERE mem_fts_stem MATCH ? AND m.deleted_at IS NULL "
            "ORDER BY bm25(mem_fts_stem) LIMIT ?",
            (fts_query, candidates),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # Log so a broken FTS index isn't silently treated as "no conflicts".
        log.warning("find_conflicts FTS query failed (%s); treating as empty", exc)
        return []

    conflicts: list[dict[str, Any]] = []
    for row in rows:
        if exclude_slug and row["slug"] == exclude_slug:
            continue
        other = _word_bag(row["title"] + "\n" + row["body"])
        if not other:
            continue
        inter = bag & other
        denom = min(len(bag), len(other))
        if not denom:
            continue
        overlap = len(inter) / denom
        if overlap >= threshold:
            conflicts.append({
                "slug": row["slug"],
                "title": row["title"],
                "overlap": round(overlap, 3),
            })
    return conflicts


# --------------------------------------------------------------------------- #
# briefing / inject
# --------------------------------------------------------------------------- #

_CHARS_PER_TOKEN = 4  # GPT-ish heuristic; we don't ship a tokenizer
_INJECT_KIND_ORDER = ["user", "feedback", "reference", "project", "note", "document"]


def briefing(
    conn: sqlite3.Connection,
    *,
    kinds: list[str] | None = None,
    budget_tokens: int = 2000,
    per_kind_limit: int = 30,
) -> dict[str, Any]:
    """Return a compact title-only briefing under a token budget.

    Format suited for a SessionStart hook: one line per memory, ordered by
    kind (user first, then feedback, then reference, ...). When the budget is
    hit we stop and report how many were omitted.
    """
    kinds = kinds or ["user", "feedback"]
    char_budget = budget_tokens * _CHARS_PER_TOKEN
    sections: list[dict[str, Any]] = []
    used = 0
    omitted = 0

    ordered = [k for k in _INJECT_KIND_ORDER if k in kinds] + [
        k for k in kinds if k not in _INJECT_KIND_ORDER
    ]

    for kind in ordered:
        rows = conn.execute(
            """
            SELECT slug, title, updated_at FROM memory_items
            WHERE kind = ? AND deleted_at IS NULL
            ORDER BY updated_at DESC LIMIT ?
            """,
            (kind, per_kind_limit),
        ).fetchall()
        if not rows:
            continue

        entries: list[dict[str, Any]] = []
        for r in rows:
            line = f"- [{r['slug']}] {r['title']}"
            cost = len(line) + 1
            if used + cost > char_budget:
                omitted += 1
                continue
            entries.append({"slug": r["slug"], "title": r["title"]})
            used += cost

        if entries:
            sections.append({"kind": kind, "items": entries})

    return {
        "sections": sections,
        "approx_tokens": used // _CHARS_PER_TOKEN,
        "omitted": omitted,
        "budget_tokens": budget_tokens,
    }


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted,
               SUM(wordcount) AS total_words
        FROM memory_items
        """
    ).fetchone()
    by_kind = conn.execute(
        """
        SELECT kind, COUNT(*) AS n FROM memory_items
        WHERE deleted_at IS NULL GROUP BY kind ORDER BY n DESC
        """
    ).fetchall()
    fts_count = conn.execute("SELECT COUNT(*) AS n FROM mem_fts").fetchone()["n"]
    skill_rows = conn.execute(
        "SELECT COUNT(*) AS n FROM memory_items WHERE kind = 'skill' AND deleted_at IS NULL"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "deleted": row["deleted"] or 0,
        "total_words": row["total_words"] or 0,
        "by_kind": {r["kind"]: r["n"] for r in by_kind},
        "skills": skill_rows["n"],
        "fts_count": fts_count,
        "history_rows": conn.execute("SELECT COUNT(*) AS n FROM memory_history").fetchone()["n"],
        "link_rows": conn.execute("SELECT COUNT(*) AS n FROM mem_links").fetchone()["n"],
    }


# --------------------------------------------------------------------------- #
# skill learning: reinforce, decay, recall
# --------------------------------------------------------------------------- #

STRENGTH_BOOST = 0.15
STRENGTH_CAP = 2.0
DECAY_FACTOR = 0.85
DECAY_FLOOR = 0.05
# Lifecycle thresholds (Hermes-inspired): active -> stale -> archived. Archived
# skills are excluded from recall but never deleted — restorable one command.
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90


def reinforce(conn: sqlite3.Connection, slug: str) -> dict[str, Any] | None:
    """Bump strength + access_count when a skill is retrieved and used."""
    row = conn.execute(
        "SELECT id, strength, access_count FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row:
        return None
    now = _now()
    new_strength = min(STRENGTH_CAP, row["strength"] + STRENGTH_BOOST)
    new_count = row["access_count"] + 1
    conn.execute(
        "UPDATE memory_items SET strength = ?, access_count = ?, last_accessed_at = ? WHERE id = ?",
        (new_strength, new_count, now, row["id"]),
    )
    return {"slug": slug, "strength": round(new_strength, 3), "access_count": new_count}


def decay_stale(
    conn: sqlite3.Connection,
    *,
    days_threshold: int = 14,
    kind: str = "skill",
) -> list[dict[str, Any]]:
    """Ebbinghaus decay: reduce strength of skills not accessed recently."""
    cutoff = _now() - days_threshold * 86400
    rows = conn.execute(
        "SELECT id, slug, strength, last_accessed_at FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND strength > ? "
        "AND (last_accessed_at IS NULL OR last_accessed_at < ?)",
        (kind, DECAY_FLOOR, cutoff),
    ).fetchall()
    decayed: list[dict[str, Any]] = []
    for r in rows:
        new_strength = max(DECAY_FLOOR, r["strength"] * DECAY_FACTOR)
        conn.execute(
            "UPDATE memory_items SET strength = ? WHERE id = ?",
            (new_strength, r["id"]),
        )
        decayed.append({
            "slug": r["slug"],
            "old_strength": round(r["strength"], 3),
            "new_strength": round(new_strength, 3),
        })
    return decayed


def _backup_skills(rows: list[sqlite3.Row], reason: str) -> None:
    """Append a human-readable JSONL snapshot before archiving (Hermes-style
    pre-prune backup). Best-effort: a write failure never blocks archiving."""
    if not rows:
        return
    try:
        bdir = default_data_dir() / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        path = bdir / "skills-archived.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({
                    "ts": _now(), "reason": reason, "slug": r["slug"],
                    "title": r["title"], "strength": r["strength"],
                    "last_accessed_at": r["last_accessed_at"],
                }, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.warning("could not write skills backup: %s", exc)


def sweep_lifecycle(
    conn: sqlite3.Connection, *, kind: str = "skill"
) -> dict[str, list[str]]:
    """Transition skills active -> stale -> archived by idle time.

    - stale:    untouched > STALE_AFTER_DAYS, currently 'active'
    - archived: untouched > ARCHIVE_AFTER_DAYS AND strength at the decay floor
                (fully faded) — backed up first, never deleted.
    Archived skills are excluded from recall (see _bm25_ids/_vector_ids).
    """
    now = _now()
    stale_cut = now - STALE_AFTER_DAYS * 86400
    archive_cut = now - ARCHIVE_AFTER_DAYS * 86400

    # COALESCE: a never-recalled skill counts idle time from its creation.
    archive_rows = conn.execute(
        "SELECT id, slug, title, strength, last_accessed_at FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND lifecycle != 'archived' "
        "AND strength <= ? AND COALESCE(last_accessed_at, created_at) < ?",
        (kind, DECAY_FLOOR, archive_cut),
    ).fetchall()
    _backup_skills(archive_rows, "archive")
    archived = [r["slug"] for r in archive_rows]
    for r in archive_rows:
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'archived' WHERE id = ?", (r["id"],)
        )

    stale_rows = conn.execute(
        "SELECT id, slug FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND lifecycle = 'active' "
        "AND COALESCE(last_accessed_at, created_at) < ?",
        (kind, stale_cut),
    ).fetchall()
    staled = [r["slug"] for r in stale_rows]
    for r in stale_rows:
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'stale' WHERE id = ?", (r["id"],)
        )
    conn.commit()
    return {"staled": staled, "archived": archived}


def lifecycle_counts(conn: sqlite3.Connection, *, kind: str = "skill") -> dict[str, int]:
    """Count skills per lifecycle state."""
    rows = conn.execute(
        "SELECT lifecycle, COUNT(*) c FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL GROUP BY lifecycle",
        (kind,),
    ).fetchall()
    return {r["lifecycle"]: r["c"] for r in rows}


def restore_skill(conn: sqlite3.Connection, slug: str) -> bool:
    """Bring an archived/stale skill back to 'active' (and nudge strength up)."""
    row = conn.execute(
        "SELECT id FROM memory_items WHERE slug = ? AND deleted_at IS NULL", (slug,)
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE memory_items SET lifecycle = 'active', "
        "strength = MAX(strength, ?), last_accessed_at = ? WHERE id = ?",
        (0.5, _now(), row["id"]),
    )
    conn.commit()
    return True


# Curator threshold (Phase 3): skills above this cosine are merge candidates.
DUP_COSINE = 0.85


def find_duplicate_skills(
    conn: sqlite3.Connection, *, threshold: float = DUP_COSINE
) -> list[dict[str, Any]]:
    """Deterministic near-duplicate detection over skill embeddings.

    Read-only. Returns candidate pairs (cosine >= threshold) for a curator to
    review/merge. Brute-force pairwise cosine is trivial at our scale. This is
    the safe foundation of the idle-fork curator — the LLM merge step consumes
    these pairs; it never invents pairs.
    """
    from . import embed as _embed

    if not _embed.available():
        return []
    import numpy as np

    rows = conn.execute(
        "SELECT id, slug, title, strength, embedding FROM memory_items "
        "WHERE kind = 'skill' AND deleted_at IS NULL AND lifecycle != 'archived' "
        "AND embedding IS NOT NULL"
    ).fetchall()
    if len(rows) < 2:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])
    sims = mat @ mat.T  # all pre-normalized -> cosine matrix
    pairs: list[dict[str, Any]] = []
    n = len(rows)
    for i in range(n):
        for j in range(i + 1, n):
            c = float(sims[i, j])
            if c >= threshold:
                pairs.append({
                    "a": rows[i]["slug"], "b": rows[j]["slug"],
                    "a_title": rows[i]["title"], "b_title": rows[j]["title"],
                    "a_strength": rows[i]["strength"], "b_strength": rows[j]["strength"],
                    "cosine": round(c, 3),
                })
    pairs.sort(key=lambda p: -p["cosine"])
    return pairs


RECALL_SQL = """
SELECT m.*, bm25(mem_fts_stem) AS bm25_rank
FROM mem_fts_stem
JOIN memory_items m ON m.id = mem_fts_stem.rowid
WHERE mem_fts_stem MATCH ?
  AND m.deleted_at IS NULL
  AND m.kind = 'skill'
ORDER BY bm25(mem_fts_stem) * (1.0 + m.strength * 0.3)
LIMIT ?
"""


def recall_skills(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    auto_reinforce: bool = True,
) -> list[dict[str, Any]]:
    """Find relevant skills for a task, optionally reinforcing them.

    Hybrid BM25+vector fusion (RRF), then a strength bonus so frequently-useful
    skills surface higher — same intent as the old ``bm25 * (1+strength*0.3)``
    ranking, now applied to the fused score. Degrades to BM25-only when no
    embeddings are present (see hybrid_rank_ids).
    """
    bm = _bm25_ids(conn, query, kind="skill")
    vec = _vector_ids(conn, query, kind="skill")
    fused = _rrf_scores(bm, vec) if vec else {i: 1.0 / (RRF_K + r + 1) for r, i in enumerate(bm)}
    if not fused:
        return []
    # apply strength bonus, then take top `limit`
    strength_by_id = {
        row["id"]: row["strength"]
        for row in conn.execute(
            f"SELECT id, strength FROM memory_items WHERE id IN ({','.join('?' * len(fused))})",
            list(fused),
        ).fetchall()
    }
    ranked_ids = sorted(
        fused, key=lambda i: -fused[i] * (1.0 + strength_by_id.get(i, 0.0) * SKILL_STRENGTH_COEF)
    )[:limit]
    placeholders = ",".join("?" * len(ranked_ids))
    fetched = {
        row["id"]: row
        for row in conn.execute(
            f"SELECT * FROM memory_items WHERE id IN ({placeholders})", ranked_ids
        ).fetchall()
    }
    rows = [fetched[i] for i in ranked_ids if i in fetched]
    now = _now()
    results: list[dict[str, Any]] = []
    for row in rows:
        d = {
            "slug": row["slug"],
            "title": row["title"],
            "body": row["body"],
            "strength": row["strength"],
            "access_count": row["access_count"],
            "score": round(fused[row["id"]], 5),
            "freshness": _freshness(now, row["updated_at"], row["freshness_until"])[0],
            # Access-control fields. Callers that serve more than one principal
            # (the HTTP layer) filter on these; omitting them made every skill
            # look public to server._visible_to and leaked private bodies.
            "visibility": row["visibility"],
            "agent": row["agent"],
            "topics": _parse_json_list(row["topics"]),
        }
        if row["body_path"]:
            item = MemoryItem.from_row(row)
            d["body"] = load_body(item)
        if auto_reinforce:
            r = reinforce(conn, row["slug"])
            if r:
                d["strength"] = r["strength"]
                d["access_count"] = r["access_count"]
        results.append(d)
    return results


def log_recall_trace(
    conn: sqlite3.Connection,
    *,
    subject_id: str | None,
    agent: str | None,
    query: str,
    results: list[dict[str, Any]],
) -> None:
    """Record one recall event (Phase 4 groundwork). Best-effort, never raises.

    Stores which skills were surfaced for a query so a future optimizer can see
    real usage. Lightweight: one INSERT per recall call.
    """
    try:
        recalled = json.dumps(
            [{"slug": r.get("slug"), "score": r.get("score")} for r in results],
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO skill_traces (subject_id, agent, query, recalled, created_at) "
            "VALUES (?,?,?,?,?)",
            (subject_id, agent, scrub(query[:500]), recalled, _now()),
        )
    except sqlite3.Error as exc:
        log.debug("recall trace write skipped: %s", exc)


def trace_stats(conn: sqlite3.Connection, *, days: int = 30) -> dict[str, Any]:
    """Aggregate recall traces for review: volume + most/least surfaced skills."""
    since = _now() - days * 86400
    total = conn.execute(
        "SELECT COUNT(*) FROM skill_traces WHERE created_at >= ?", (since,)
    ).fetchone()[0]
    empty = conn.execute(
        "SELECT COUNT(*) FROM skill_traces WHERE created_at >= ? AND recalled = '[]'",
        (since,),
    ).fetchone()[0]
    # per-skill surface counts (parse JSON in Python — small volumes)
    counts: dict[str, int] = {}
    for (recalled,) in conn.execute(
        "SELECT recalled FROM skill_traces WHERE created_at >= ?", (since,)
    ):
        for item in _parse_json_list_objs(recalled):
            slug = item.get("slug")
            if slug:
                counts[slug] = counts.get(slug, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:15]
    return {"days": days, "events": total, "empty_recalls": empty,
            "distinct_skills_surfaced": len(counts), "top": top}


def _parse_json_list_objs(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []
