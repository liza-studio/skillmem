"""SQLite + FTS5 storage layer for skillmem.

Full record provenance (birth certificate / supersession / death record)
is built in:
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
from typing import Callable, Any, Iterable, Iterator

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
# Extended secret patterns. High-precision — low false-positive risk.
_PEM_KEY = _re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    _re.DOTALL,
)
_AWS_KEY = _re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_TG_BOT_TOKEN = _re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{32,}\b")
_JWT = _re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")
# password=... / "token": "..." / secret: ... — redact the value, keep the key name.
# A value that is already a redaction marker is not a secret: without the
# lookahead every re-write of a scrubbed body (dump restore, mem_update,
# /update) grew "[secret redacted] redacted]", changed the content hash and
# dropped the row's approval.
_SECRET_ASSIGN = _re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|api[_\-]?key|token|access[_\-]?token)\b"""
    r"""(\s*[:=]\s*)(["']?)(?!\[[a-z\-]+ redacted\])([^\s"',;]{6,})(["']?)""",
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
    pinned          INTEGER NOT NULL DEFAULT 0,    -- 1 = never decays, never archived (v9)
    confirmed_count INTEGER NOT NULL DEFAULT 0,    -- times an external signal confirmed it (v9)
    failure_count   INTEGER NOT NULL DEFAULT 0,    -- times it was followed by a failure (v9)
    origin          TEXT NOT NULL DEFAULT 'unknown', -- owner|agent|imported|derived (v10)
    trusted_at      INTEGER,                       -- set only by the owner (v10)
    trusted_by      TEXT,
    owner_seal      INTEGER NOT NULL DEFAULT 0,    -- was the owner's; never cleared (0.11.1)
    last_decayed_at INTEGER,                       -- one decay step per threshold (0.11)
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    deleted_at      INTEGER,
    embedding       BLOB                           -- float32×384, semantic recall (v6)
);

CREATE INDEX IF NOT EXISTS idx_memory_kind     ON memory_items(kind);
CREATE INDEX IF NOT EXISTS idx_memory_project  ON memory_items(project);
CREATE INDEX IF NOT EXISTS idx_memory_updated  ON memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_memory_hash     ON memory_items(content_hash);

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
"""


CURRENT_SCHEMA_VERSION = 11


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema bootstrap. Cheap on every call after first run."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    # No write here: _migrate records schema_version itself. An INSERT on every
    # open took the write lock, so a hook opening the DB behind any writer
    # stalled for the whole busy_timeout and then emitted nothing.


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


def _add_column(conn: sqlite3.Connection, ddl: str) -> None:
    """ALTER ... ADD COLUMN that tolerates losing the race to another process.

    Two hooks opening a pre-v9 DB at once both saw the column missing and both
    ALTERed; the loser died with "duplicate column name". v10 guarded this,
    the older columns did not.
    """
    try:
        conn.execute(f"ALTER TABLE memory_items ADD COLUMN {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only schema patches. Backfills run once, then are skipped."""
    # Self-healing guard: ensure the embedding column exists regardless of the
    # version gate below. A migration interrupted between bumping the version
    # and running its ALTER (e.g. a concurrent recall hook) could otherwise
    # strand the DB at v6 with no column. Cheap idempotent check on every open.
    live_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "embedding" not in live_cols:
        _add_column(conn, "embedding BLOB")
    # v7: skill lifecycle state (active -> stale -> archived). Self-healing,
    # like embedding, so the column is guaranteed regardless of the version gate.
    if "lifecycle" not in live_cols:
        _add_column(conn, "lifecycle TEXT NOT NULL DEFAULT 'active'")
    # 0.11: one decay step per threshold (see decay_stale). Self-healing, like
    # the columns above — the schema version does not move for it.
    if "last_decayed_at" not in live_cols:
        _add_column(conn, "last_decayed_at INTEGER")
    # 0.11: kinds are normalised on write now; rows written before that as
    # "Reference" would be invisible to a kind="reference" filter. Read first
    # so the common case takes no write lock on open.
    odd = conn.execute(
        "SELECT id, kind FROM memory_items WHERE kind != LOWER(TRIM(kind, ' \t\r\n')) "
        "OR kind LIKE '%  %' OR kind LIKE '%' || char(9) || '%' "
        "OR kind LIKE '%' || char(10) || '%' OR kind LIKE '%' || char(13) || '%' "
        "OR kind LIKE '%' || char(11) || '%' OR kind LIKE '%' || char(12) || '%' "
        "OR kind LIKE '%' || char(160) || '%'"
    ).fetchall()
    for r in odd:
        # the same normalisation writes get (case, trim, whitespace collapse);
        # a value that still fails validation is left for the owner to fix
        norm = _re.sub(r"\s+", " ", str(r["kind"]).strip().lower())
        if norm != r["kind"]:
            conn.execute("UPDATE memory_items SET kind = ? WHERE id = ?", (norm, r["id"]))
    # 0.11: visibility is validated on write; a pre-0.11 row with an off-enum
    # value ("team", "../x") could no longer be updated at all. Repair to the
    # safe default — private — once, on open.
    bad = conn.execute(
        "SELECT id FROM memory_items WHERE LOWER(TRIM(visibility)) "
        "NOT IN ('public', 'shared', 'private') "
        "OR visibility != LOWER(TRIM(visibility)) LIMIT 1"   # same test as the UPDATE
    ).fetchone()
    if bad is not None:
        conn.execute(
            "UPDATE memory_items SET visibility = CASE "
            "WHEN LOWER(TRIM(visibility)) IN ('public','shared','private') "
            "THEN LOWER(TRIM(visibility)) ELSE 'private' END "
            "WHERE LOWER(TRIM(visibility)) NOT IN ('public','shared','private') "
            "OR visibility != LOWER(TRIM(visibility))"
        )
    # v10, same self-healing reason: a DB whose version says 10 but whose ALTER
    # never landed would otherwise fail every read with "no such column: origin".
    if not {"origin", "trusted_at", "trusted_by"} <= live_cols:
        _migrate_v10(conn)
    # after v10: the backfill reads origin and trusted_at, and repairs a database
    # whose column landed without them
    _migrate_owner_seal(conn)

    if _current_schema_version(conn) >= CURRENT_SCHEMA_VERSION:
        return

    cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "body_path" not in cols:
        _add_column(conn, "body_path TEXT")
    if "stemmed" not in cols:
        _add_column(conn, "stemmed TEXT NOT NULL DEFAULT ''")

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
        _add_column(conn, "access_count INTEGER NOT NULL DEFAULT 0")
    if "last_accessed_at" not in cols:
        _add_column(conn, "last_accessed_at INTEGER")

    # --- v6: semantic embedding column ---
    # Column is added empty; backfill is a separate, optional, network-bound
    # step (`skillmem reindex-embeddings`) so the migration never blocks on a
    # model download. Recall falls back to BM25 for rows without an embedding.
    if "embedding" not in cols:
        _add_column(conn, "embedding BLOB")

    # --- v8: drop the legacy porter FTS index ---
    # mem_fts was superseded by mem_fts_stem (Snowball) and had no readers
    # left, yet its three triggers doubled the FTS work on every write.
    conn.executescript(
        """
        DROP TRIGGER IF EXISTS memory_items_ai;
        DROP TRIGGER IF EXISTS memory_items_ad;
        DROP TRIGGER IF EXISTS memory_items_au;
        DROP TABLE IF EXISTS mem_fts;
        """
    )

    # --- v9: evidence-weighted reinforcement ---
    # `pinned` exempts a rule from decay and archiving: a rule that matters
    # precisely because it is rarely needed ("deploy only through the gate")
    # must not fade at the same rate as a note nobody reads. The two counters
    # keep confirmations and failures apart from raw retrieval count, so a
    # skill's strength can be traced back to what actually confirmed it.
    if "pinned" not in cols:
        _add_column(conn, "pinned INTEGER NOT NULL DEFAULT 0")
    if "confirmed_count" not in cols:
        _add_column(conn, "confirmed_count INTEGER NOT NULL DEFAULT 0")
    if "failure_count" not in cols:
        _add_column(conn, "failure_count INTEGER NOT NULL DEFAULT 0")

    # --- v10: provenance, and trust as an explicit act ---
    # The loop this closes: an external text (a README, a web page) reaches a
    # transcript, a model distils it into a note, and the note comes back as a
    # rule in the next session. Origin records where text came from; trust is
    # only ever granted by the owner, because an agent can be talked into
    # storing a rule by the very document it was reading.
    if not {"origin", "trusted_at", "trusted_by"} <= cols:
        _migrate_v10(conn)

    # --- v11: the lexical index has to be rebuilt, but NOT here ---
    # It used to drop tokens shorter than three characters, so `db`, `py`, `js`,
    # `ci` were missing from every row and a query like "db.py" could not match
    # however well the query itself was tokenised. The column is derived, so the
    # fix only reaches stored memories by rebuilding it — and on a real database
    # (8917 rows) that takes about a minute, while init_schema runs inside every
    # hook under a 10s timeout. So the migration only leaves a flag: the nightly
    # decay job picks it up, or `skillmem reindex-lexical` does it now.
    if _current_schema_version(conn) < 11 and conn.execute(
            "SELECT EXISTS(SELECT 1 FROM memory_items)").fetchone()[0]:
        # Only an existing database has a stale index to rebuild; a fresh one is
        # already correct, and flagging it would send the nightly job on a
        # pointless pass and make `doctor` look alarming on a clean install.
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES "
            "('lexical_reindex_pending', '1')")

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION),),
    )


# Origin of existing rows, in priority order — the first match wins, so a skill
# carrying the imported tag is imported, not agent.
def lexical_reindex_pending(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'lexical_reindex_pending'").fetchone()
    return bool(row and str(row["value"]) == "1")


def restem_all(conn: sqlite3.Connection) -> int:
    """Rebuild the lexical index and clear the pending flag. Minutes, not seconds,
    on a large database — call it from a scheduled job or by hand, never from a
    hook."""
    n = _restem_all(conn)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                 "('lexical_reindex_pending', '0')")
    return n


def _restem_all(conn: sqlite3.Connection) -> int:
    """Rebuild the stemmed column for every row. No model, no network, one pass."""
    rows = conn.execute(
        "SELECT * FROM memory_items"
    ).fetchall()
    n = 0
    with tx(conn):
        for r in rows:
            # the DB body is only an excerpt for externalized documents; index
            # the whole text or the tail stops matching after every reindex
            body = load_body(MemoryItem.from_row(r)) if r["body_path"] else r["body"]
            stemmed = _stem_text(
                f"{r['title']}\n{body}\n"
                + " ".join(_parse_json_list(r["tags"]) + _parse_json_list(r["topics"]))
            )
            conn.execute("UPDATE memory_items SET stemmed = ? WHERE id = ?",
                         (stemmed, r["id"]))
            n += 1
    log.info("re-stemmed %d rows for v11", n)
    return n


_ORIGIN_BACKFILL = (
    ("derived", "kind = 'note'"),
    ("owner", "kind IN ('user','feedback','rule','reference','project')"),
    ("agent", "kind = 'skill'"),
)


_V10_COLUMNS = (
    ("origin", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("trusted_at", "INTEGER"),
    ("trusted_by", "TEXT"),
)


def _backup_before_v10(conn: sqlite3.Connection) -> None:
    """A copy of the database before its first structural change of this release.

    Cheap insurance the changelog promises: SQLite's own backup API, so a WAL in
    flight cannot produce a torn copy. A failure here must not block the upgrade —
    the migration itself is additive.
    """
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not path:
            return  # :memory:
        dest = Path(path).parent / "backups" / f"pre-v10-{int(time.time())}.db"
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(dest)) as out:
            conn.backup(out)
        log.info("pre-v10 backup written to %s", dest)
    except Exception as exc:  # noqa: BLE001 - never block the upgrade
        log.warning("could not write the pre-v10 backup: %s", exc)


def _migrate_owner_seal(conn: sqlite3.Connection) -> None:
    """Add owner_seal and seal what the owner already wrote or approved.

    Set once, never cleared: this record was the owner's, whatever happens to it
    later. `origin` and `trusted_at` both move under an agent's own writes
    (mem_update relabels origin to 'agent' and drops the approval, by design), so
    neither can carry a rule the same agent must not be able to lift. Checked on
    every open, like v10's columns: a database carrying v10 already never runs
    that migration again, and this column has to reach those databases too.
    """
    try:
        with tx(conn):   # BEGIN IMMEDIATE: two processes may open the same file
            have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
            if "owner_seal" not in have:
                conn.execute(
                    "ALTER TABLE memory_items ADD COLUMN owner_seal "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # Run the backfill whether or not the column is new: an interrupted
            # migration can leave the column present and unsealed, and taking
            # presence as proof of the backfill would never repair that.
            conn.execute(
                "UPDATE memory_items SET owner_seal = 1 "
                "WHERE owner_seal = 0 AND (origin = 'owner' OR trusted_at IS NOT NULL)"
            )
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Add provenance + approval, atomically, once.

    Two processes can open the same database at the same moment (a recall hook
    and the CLI), so the columns are re-checked after the write lock is held:
    without that the loser of the race dies on a duplicate column, and a partial
    set of columns would then fail every read.
    """
    _backup_before_v10(conn)
    try:
        with tx(conn):  # BEGIN IMMEDIATE — the lock is held for the whole change
            have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
            added = False
            for name, decl in _V10_COLUMNS:
                if name not in have:
                    conn.execute(f"ALTER TABLE memory_items ADD COLUMN {name} {decl}")
                    added = True
            if added:
                _backfill_origin(conn)
    except sqlite3.OperationalError as exc:
        # Another process finished the same migration between our check and the
        # lock; anything else is a real problem worth surfacing.
        if "duplicate column" not in str(exc).lower():
            raise


def _classify_tags(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    """Split rows into (imported, unreadable) by their tag list.

    Deliberately not json_each: a missing JSON1 extension or one malformed list
    used to fail the whole rule, and those rows then fell through to the
    kind-based rules — which turned someone else's pack into a trusted rule.

    A row whose tags will not parse is not guessed at either way: truncated JSON
    can hide the marker entirely (`'["imported",'`), so it is labelled `unknown`,
    which is never grandfathered. An upgrade that leaves a handful of rows
    needing `skillmem trust` is a cost; approving a pack silently is not.
    """
    imported: list[int] = []
    unreadable: list[int] = []
    for row in conn.execute("SELECT id, tags FROM memory_items"):
        raw = row["tags"]
        if raw in (None, "", "[]"):
            continue
        try:
            tags = json.loads(raw) if isinstance(raw, str) else list(raw)
            if not isinstance(tags, list):
                raise ValueError("tags is not a list")
        except Exception:
            unreadable.append(row["id"])
            continue
        if "untrusted-origin" in tags or any(str(t).startswith("pack:") for t in tags):
            imported.append(row["id"])
    return imported, unreadable


def _backfill_origin(conn: sqlite3.Connection) -> None:
    """Label existing rows, then grandfather only what the owner accumulated.

    Grandfathering is a deliberate, stated compromise: the alternative is that
    every rule the owner has relied on for months arrives unapproved on the
    morning after an upgrade. Imported packs and transcript summaries never get it.
    """
    now = int(time.time())
    imported, unreadable = _classify_tags(conn)
    if imported:
        marks = ",".join("?" * len(imported))
        conn.execute(
            f"UPDATE memory_items SET origin = 'imported' WHERE id IN ({marks})",
            imported)
    # Rows whose tags we could not read keep origin='unknown' and are held back
    # from the kind rules below: we cannot see their provenance, so we neither
    # invent one nor approve them.
    skip = "" if not unreadable else (
        f" AND id NOT IN ({','.join('?' * len(unreadable))})")
    for origin, clause in _ORIGIN_BACKFILL:
        conn.execute(
            f"UPDATE memory_items SET origin = ? WHERE origin = 'unknown' "
            f"AND ({clause}){skip}", (origin, *unreadable))
    # Grandfather only what the owner accumulated themselves: their own notes and
    # rules, and the skills their own sessions learned. Never an imported pack,
    # and never a transcript summary — `derived` is precisely the class this
    # release exists to distrust, and approving 7809 of them at once would empty
    # the marker of meaning on day one.
    conn.execute(
        # owner_seal is NOT set here: this runs inside the v10 migration, before
        # that column exists, and SQLite resolves names at prepare time.
        # _migrate_owner_seal seals these same rows right afterwards.
        "UPDATE memory_items SET trusted_at = ?, trusted_by = 'migration-v10' "
        "WHERE trusted_at IS NULL AND origin IN ('owner', 'agent')", (now,))


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


_CLOCK_WARNED: list[bool] = []


def _chain_clock(conn: sqlite3.Connection, now: int) -> int:
    """The chain is walked in (changed_at, id) order, so a row stamped earlier
    than its predecessor (clock stepped back) would verify as a break. Clamp
    the timestamp to the tip instead of reordering existing chains."""
    row = conn.execute("SELECT MAX(changed_at) AS t FROM memory_history").fetchone()
    tip = int(row["t"]) if row and row["t"] is not None else 0
    if tip - now > 86400 and not _CLOCK_WARNED:
        # a row stamped far in the future (clock was wrong) pins every later
        # stamp to it until real time catches up — by design (the chain
        # must stay ordered). One line per process, not per write.
        _CLOCK_WARNED.append(True)
        log.warning("history clock: tip is %d s ahead of now; clamping", tip - now)
    return max(now, tip)


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

    Single characters are dropped as BM25 noise; two-character tokens are kept
    because in this domain they carry the meaning — `db`, `py`, `js`, `ci`, `ui`,
    `go`. Dropping them made a query like "db.py" match nothing at all, however
    well the query side was tokenised. Stems are joined by spaces; punctuation is
    discarded — Snowball is what gives lexical recall here, FTS5 just BM25-ranks
    the result, and a frequent short word is down-weighted by BM25 anyway.
    """
    if not text:
        return ""
    out: list[str] = []
    for match in _WORD_RE.finditer(text):
        w = match.group(0)
        if len(w) < 2:
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
    pinned: bool = False
    lifecycle: str = "active"
    owner_seal: int = 0
    confirmed_count: int = 0
    failure_count: int = 0
    access_count: int = 0
    last_accessed_at: int | None = None
    # Where the text came from — never a judgement, just a fact:
    # owner (a human typed it) / agent (an agent stored it mid-session) /
    # imported (someone else's pack) / derived (a model's summary of a
    # transcript) / unknown.
    origin: str = "unknown"
    # Trust is an explicit act by the owner, not a guess from origin: an agent
    # can be talked into storing a rule by a README it was reading.
    trusted_at: int | None = None
    trusted_by: str | None = None
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
            pinned=bool(row["pinned"]) if "pinned" in row.keys() else False,
            lifecycle=(row["lifecycle"] if "lifecycle" in row.keys() else "active"),
            owner_seal=(row["owner_seal"] if "owner_seal" in row.keys() else 0),
            confirmed_count=(row["confirmed_count"]
                             if "confirmed_count" in row.keys() else 0),
            failure_count=row["failure_count"] if "failure_count" in row.keys() else 0,
            access_count=row["access_count"] if "access_count" in row.keys() else 0,
            last_accessed_at=row["last_accessed_at"] if "last_accessed_at" in row.keys() else None,
            origin=(row["origin"] if "origin" in row.keys() and row["origin"]
                    else "unknown"),
            trusted_at=row["trusted_at"] if "trusted_at" in row.keys() else None,
            trusted_by=row["trusted_by"] if "trusted_by" in row.keys() else None,
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
    # keep the key name and separator, mask only the value
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


def _db_identity(conn: sqlite3.Connection) -> str:
    """8 hex chars of the resolved database path — no default special case."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(Path(row[2]).resolve()) if row and row[2] else ":memory:"
    except (sqlite3.Error, OSError, TypeError):
        path = ":memory:"
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]


def _db_namespace(conn: sqlite3.Connection) -> str:
    """8 hex chars naming the database a body file belongs to.

    docs/ is shared by every database under one SKILLMEM_HOME, and the file
    name used to depend on the slug alone — so two databases with the same
    slug read and overwrote one file. The default database keeps the empty
    namespace so existing files stay valid; any other database gets its own.
    """
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        path = str(Path(row[2]).resolve()) if row and row[2] else f":memory:{id(conn)}"
    except (sqlite3.Error, OSError, TypeError):
        path = f":memory:{id(conn)}"
    # Only the canonical file keeps the empty namespace — NOT whatever
    # SKILLMEM_DB points at, or an override DB and memory.db would share it.
    canonical = str((default_data_dir() / "memory.db").resolve())
    if not path or path == canonical:
        return ""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()[:8]


def _body_filename(slug: str, *, ns: str = "", content_hash: str = "") -> str:
    """``<safe-slug>__<hash8>[-<ns8>][+<content32>].md``.

    Content-addressed: a new body is a NEW file, never an overwrite of the one
    a committed row points at. So publishing before COMMIT is safe — a rollback
    leaves an orphan, which gc_body_files() collects, and never a row whose
    file holds someone else's text.
    """
    safe = _re.sub(r"[^\w.\-]+", "-", slug, flags=_re.UNICODE).strip("-") or "untitled"
    h = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    if ns:
        h += f"-{ns}"
    if content_hash:
        # 32 hex = 128 bits. Eight used to be enough to look unique and not
        # be: two bodies sharing a prefix shared a file, and a rollback then
        # left a row pointing at the other body's text.
        h += f"+{content_hash[:32]}"
    return f"{safe}__{h}.md"


_BODY_FILE_RE = _re.compile(
    r"__[0-9a-f]{8}(?P<ns>-[0-9a-f]{8})?(?P<content>\+[0-9a-f]{8,64})?\.md$"
)


def _stage_body_file(conn: sqlite3.Connection, slug: str, body: str,
                     content_hash: str) -> tuple[Path, Path]:
    """Write the new body to a scratch file and publish it under its own name.

    The name carries the content hash, so this never touches the file a
    committed row references; the row is switched to it by the transaction
    that follows. If that transaction (or an outer one wrapping it) rolls
    back, the row keeps pointing at the old file and this one is an orphan
    for gc_body_files() — the two can no longer disagree.
    """
    dest = docs_dir() / _body_filename(slug, ns=_db_namespace(conn),
                                       content_hash=content_hash)
    tmp = dest.with_suffix(dest.suffix + f".staged-{_uuid.uuid4().hex[:8]}")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, dest)
    return dest, dest


def _publish_body_file(staged: tuple[Path, Path]) -> str:
    return staged[1].name


def _discard_body_file(conn: sqlite3.Connection,
                       staged: tuple[Path, Path] | None) -> None:
    """A failed transaction leaves its (content-addressed) file for gc.

    It must not unlink: with identical content two writers share one file,
    and the one that lost on "database is locked" cannot see the winner's
    uncommitted row — it would delete the file the winner is about to
    reference. gc_body_files() removes true orphans after a grace period.
    """
    return None


def gc_body_files(conn: sqlite3.Connection) -> int:
    """Delete body files this database no longer references. Returns count.

    Only files in this database's namespace are candidates — another
    database's files share the directory and are not ours to judge.
    """
    ns = _db_namespace(conn)
    removed = 0
    # Scan and unlink under the write lock: SQLite has one writer, so an
    # open write transaction (a vault import publishes its files as it goes,
    # then commits at the end) blocks this run instead of losing its files.
    # The 60 s grace covers only the pre-transaction staging window.
    # ponytail: the write lock spans the whole docs/ scan; split into
    # scan-outside/unlink-inside if docs/ ever holds tens of thousands of files.
    try:
        with tx(conn):
            live = {r[0] for r in conn.execute(
                "SELECT body_path FROM memory_items WHERE body_path IS NOT NULL")}
            for path in docs_dir().glob("*.md"):
                m = _BODY_FILE_RE.search(path.name)
                if m is None or path.name in live:
                    continue
                if m.group("content") is None:
                    continue  # pre-0.11 name: no namespace, could be any database's
                if (m.group("ns") or "") != (f"-{ns}" if ns else ""):
                    continue  # another database's file
                try:
                    if time.time() - path.stat().st_mtime < 60:
                        continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            log.info("gc_body_files skipped: %s", exc)  # a writer holds the lock
            return 0
        raise  # disk I/O, missing schema, read-only: not "nothing to do"
    return removed


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


ORIGINS = ("owner", "agent", "imported", "derived", "unknown")


def _valid_origin(origin: str | None) -> str:
    """Anything unrecognised is 'unknown' — and unknown is never trusted."""
    return origin if origin in ORIGINS else "unknown"


_KIND_RE = _re.compile(r"[a-z0-9_-][a-z0-9_ -]{0,31}")


def _valid_kind(kind: str) -> str:
    """kind ends up in file paths (export, vault) — keep it a plain word.

    Normalises case and whitespace first ("Reference" is a reference), so
    frontmatter and older rows keep working; only genuinely unsafe values
    ("../x", "", 40 chars) are refused.
    """
    norm = _re.sub(r"\s+", " ", (kind or "").strip().lower())
    if not _KIND_RE.fullmatch(norm):
        raise ValueError(
            f"invalid kind {kind!r}: use a-z, 0-9, space, '_' or '-', 1-32 chars"
        )
    return norm


VISIBILITIES = ("public", "shared", "private")


def _valid_visibility(visibility: str | None) -> str:
    """One rule for every channel — HTTP validates too, MCP/CLI did not."""
    v = (visibility or "private").strip().lower()
    if v not in VISIBILITIES:
        raise ValueError(f"invalid visibility {visibility!r}: one of {', '.join(VISIBILITIES)}")
    return v


def set_trust(conn: sqlite3.Connection, slug: str, *, trusted: bool,
              by: str = "owner") -> MemoryItem | None:
    """Grant or withdraw the owner's approval. The only way trust is ever set."""
    row = conn.execute(
        "SELECT id FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row:
        return None
    if trusted:
        conn.execute(
            "UPDATE memory_items SET trusted_at = ?, trusted_by = ?, owner_seal = 1 "
            "WHERE id = ?",
            (_now(), by, row["id"]),
        )
    else:
        conn.execute(
            "UPDATE memory_items SET trusted_at = NULL, trusted_by = NULL "
            "WHERE id = ?", (row["id"],),
        )
    return get(conn, slug)


def upsert(
    conn: sqlite3.Connection,
    item: MemoryItem,
    *,
    reason: str | None = None,
    force: bool = False,
    check_conflicts: bool = False,
    conflict_filter: Callable[[dict[str, Any]], bool] | None = None,
    links: Iterable[str] | None = None,
    restore_strength: bool = False,
    explicit: set[str] | None = None,
    revive: bool = False,
) -> MemoryItem:
    """Insert or update ``item`` by slug; returns the same (mutated) object.

    NOTE: ``item`` is mutated in place — title/body are scrubbed, and when the
    body is large enough to be externalized to a file, ``item.body`` is
    replaced with the short excerpt (``item.body_path`` then points at the
    full text; use ``load_body`` to read it back). Keep your own copy of the
    original body if you need it after the call.

    ``strength`` is evidence the row earned; an ordinary update keeps it. Only
    an explicit restore passes ``restore_strength=True`` (a vault import of a
    skillmem dump, or a file carrying a strength) — before that, every
    force-overwrite and every pack re-import silently reset it to 1.0.

    ``explicit`` names the metadata fields the caller actually supplied
    (``{"kind", "visibility", "tags", ...}``). On a same-text write only those
    are applied — so a retried ``mem_write`` without a ``kind`` no longer turns
    a trusted skill into a note, and an explicit ``topics=[]`` really clears
    the audience. ``None`` keeps the library-level behaviour (every non-empty
    field applies); ``agent`` is never applied to an existing row by a create.

    A soft-deleted slug is refused unless ``revive=True`` (a restore or a pack
    reinstall), so a write is never acknowledged and then invisible.
    """
    item.kind = _valid_kind(item.kind)
    item.visibility = _valid_visibility(item.visibility)
    if item.ttl_days is not None and not (1 <= int(item.ttl_days) <= 3650):
        raise ValueError(f"invalid ttl_days {item.ttl_days!r}: 1..3650")
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
        item.body_path = _body_filename(item.slug, ns=_db_namespace(conn),
                                        content_hash=item.content_hash)
        item.body = _make_excerpt(full_body)
    else:
        item.body_path = None

    stemmed = _stem_text(
        f"{item.title}\n{full_body}\n" + " ".join(item.tags + item.topics)
    )

    existing = conn.execute(
        "SELECT * FROM memory_items WHERE slug = ?", (item.slug,)
    ).fetchone()

    if existing is not None and existing["deleted_at"] is not None and not revive:
        # A tombstone is still a record. Writing onto it used to be acknowledged
        # ("OK: slug") while the row stayed invisible to every reader.
        raise MemoryConflict(
            f"slug '{item.slug}' belongs to a deleted record; restore it "
            f"(revive) or pick another slug"
        )

    if existing is None:
        # Conflict check runs BEFORE the transaction so we hold no write lock
        # while we're scanning FTS5 — keeps concurrent searchers responsive.
        if check_conflicts and not force:
            conflicts = find_conflicts(conn, item.title, item.body, visible=conflict_filter)
            if conflicts:
                raise MemoryConflict(
                    "duplicate-candidates:" + json.dumps(conflicts, ensure_ascii=False)
                )
        if item.ttl_days and not item.freshness_until:
            item.freshness_until = now + item.ttl_days * 86400
        if not item.created_at:
            item.created_at = now
        item.updated_at = now

        staged = (_stage_body_file(conn, item.slug, full_body, item.content_hash)
                  if externalize else None)
        try:
            with tx(conn):
                cur = conn.execute(
                    """
                    INSERT INTO memory_items (
                        slug, kind, title, body, body_path, stemmed, project, tags,
                        topics, visibility, agent, source_session, attachments, ttl_days,
                        freshness_until, wordcount, content_hash, supersedes_id,
                        confidence, strength, origin, trusted_at, trusted_by,
                        owner_seal, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item.slug, item.kind, item.title, item.body, item.body_path,
                        stemmed, item.project,
                        _json_list(item.tags), _json_list(item.topics), item.visibility,
                        item.agent, item.source_session, _json_list(item.attachments),
                        item.ttl_days, item.freshness_until,
                        item.wordcount, item.content_hash, item.supersedes_id,
                        item.confidence, item.strength,
                        _valid_origin(item.origin), item.trusted_at, item.trusted_by,
                        # the seal follows the owner, not the current label
                        1 if (_valid_origin(item.origin) == "owner"
                              or item.trusted_at) else 0,
                        item.created_at, item.updated_at,
                    ),
                )
                item.id = cur.lastrowid
                if links is not None:
                    _replace_links_inner(conn, item.slug, links)
        except sqlite3.IntegrityError as exc:
            _discard_body_file(conn, staged)
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
            _discard_body_file(conn, staged)
            raise
        if staged is not None:
            _publish_body_file(staged)
        _set_embedding(conn, item.id, item.title, full_body)
        return item

    if existing["content_hash"] == item.content_hash:
        # Same text — no history entry, and the owner's approval survives because
        # it was given to these words. But metadata may still have changed, and
        # returning the old row unchanged reported success for a write that never
        # happened.
        meta = {
            "project": item.project, "visibility": item.visibility,
            "kind": item.kind, "ttl_days": item.ttl_days,
        }
        if explicit is None:
            changed = {k: v for k, v in meta.items()
                       if v is not None and v != existing[k]}
            if item.agent is not None and item.agent != existing["agent"]:
                changed["agent"] = item.agent
            if _valid_origin(item.origin) == "owner":
                changed["owner_seal"] = 1
            if restore_strength and item.origin and _valid_origin(item.origin) != existing["origin"]:
                # only a RESTORE (a skillmem dump) rewrites provenance on same
                # text; an ordinary library write with the default "unknown",
                # or a migrate of a hand-written file, must not relabel a row
                changed["origin"] = _valid_origin(item.origin)
        else:
            changed = {k: v for k, v in meta.items()
                       if k in explicit and v is not None and v != existing[k]}
            if "ttl_days" in explicit and item.ttl_days is None and existing["ttl_days"] is not None:
                changed["ttl_days"] = None    # an explicit null clears the TTL (HTTP sends it as such)
        tags, topics = _json_list(item.tags), _json_list(item.topics)
        tags_given = ("tags" in explicit) if explicit is not None else bool(item.tags)
        topics_given = ("topics" in explicit) if explicit is not None else bool(item.topics)
        if tags_given and tags != existing["tags"]:
            changed["tags"] = tags
        if topics_given and topics != existing["topics"]:
            changed["topics"] = topics
        if revive and existing["deleted_at"] is not None:
            changed["deleted_at"] = None
        if "ttl_days" in changed:
            # a new TTL is a new deadline; storing ttl_days alone left
            # freshness_until as it was and the expiry never came
            ttl = changed["ttl_days"]
            changed["freshness_until"] = now + ttl * 86400 if ttl else None
        if "tags" in changed or "topics" in changed:
            # tags/topics are part of the lexical index — a tag added to an
            # unchanged body must be searchable, so the stems follow. Built
            # from the RESULTING metadata: a field not supplied keeps the
            # row's value, one supplied (even empty) replaces it.
            eff_tags = item.tags if tags_given else _parse_json_list(existing["tags"])
            eff_topics = item.topics if topics_given else _parse_json_list(existing["topics"])
            changed["stemmed"] = _stem_text(
                f"{item.title}\n{full_body}\n" + " ".join(eff_tags + eff_topics)
            )
        if restore_strength and item.strength != existing["strength"]:
            changed["strength"] = item.strength   # an explicit restore applies to same text too
        if changed:
            changed["updated_at"] = now
            sets = ", ".join(f"{k} = ?" for k in changed)
            conn.execute(f"UPDATE memory_items SET {sets} WHERE id = ?",
                         (*changed.values(), existing["id"]))
            if links is not None:
                _replace_links_inner(conn, item.slug, links)
            return get(conn, item.slug) or MemoryItem.from_row(existing)
        return MemoryItem.from_row(existing)

    if not reason and not force:
        # this reaches the CLI, MCP and HTTP alike, and the create surfaces
        # (mem_write, mem_learn) carry neither reason= nor force=; name the
        # action, not a parameter the caller may not have
        raise MemoryConflict(
            f"slug '{item.slug}' already exists with different text; "
            f"overwrite it through an explicit update, or pick another slug"
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

    # Content-addressed: the new file has its own name, the old one survives
    # any rollback untouched (see _stage_body_file).
    staged = (_stage_body_file(conn, item.slug, full_body, item.content_hash)
              if externalize else None)
    try:
        _upsert_update_tx(
            conn, item=item, existing=existing, now=now, reason=reason,
            stemmed=stemmed, freshness=freshness, old_body=old_body, links=links,
            strength=item.strength if restore_strength else None,
            revive=revive,
        )
    except BaseException:
        _discard_body_file(conn, staged)
        raise

    # The previous body file is NOT deleted here: an outer transaction (vault
    # import) may still roll this update back, and then the row needs it.
    # gc_body_files() removes unreferenced files on the nightly run.

    item.id = existing["id"]
    item.created_at = existing["created_at"]
    item.updated_at = now
    item.freshness_until = freshness
    if not restore_strength:
        item.strength = existing["strength"]   # what the row keeps, not the caller's default
    _set_embedding(conn, item.id, item.title, full_body)
    return item


def _upsert_update_tx(
    conn: sqlite3.Connection, *, item: "MemoryItem", existing: Any, now: int,
    reason: str | None, stemmed: str, freshness: int | None, old_body: str,
    links: list[str] | None, strength: float | None = None, revive: bool = False,
) -> None:
    with tx(conn):
        prev_hash = _last_chain_hash(conn)
        now = _chain_clock(conn, now)
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
                -- the owner writing over a record seals it; nothing clears it
                owner_seal = CASE WHEN ? THEN 1 ELSE owner_seal END,
                kind = ?, title = ?, body = ?, body_path = ?, stemmed = ?, project = ?,
                tags = ?, topics = ?, visibility = ?, agent = ?, source_session = ?,
                attachments = ?, ttl_days = ?, freshness_until = ?, wordcount = ?,
                content_hash = ?, supersedes_id = ?, confidence = ?,
                strength = COALESCE(?, strength),
                origin = ?,
                -- We only get here when title/body actually changed (an
                -- identical write returns early), and approval belongs to the
                -- text that was approved, not to the slug.
                trusted_at = NULL, trusted_by = NULL,
                deleted_at = CASE WHEN ? THEN NULL ELSE deleted_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                1 if (_valid_origin(item.origin) == "owner" or item.trusted_at) else 0,
                item.kind, item.title, item.body, item.body_path, stemmed, item.project,
                _json_list(item.tags), _json_list(item.topics),
                item.visibility, item.agent, item.source_session,
                _json_list(item.attachments),
                item.ttl_days, freshness, item.wordcount, item.content_hash,
                item.supersedes_id, item.confidence, strength,
                _valid_origin(item.origin), 1 if revive else 0, now,
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
    # Autocommit connection — see sweep_lifecycle for why there is no commit().
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
        now = _chain_clock(conn, now)
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
# chain verification (tamper-evident history)
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
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[MemoryItem]:
    if kind:
        try:
            kind = _valid_kind(kind)  # "Reference" filters find "reference" rows
        except ValueError:
            return []                 # a filter nothing can match matches nothing
    where = ["deleted_at IS NULL", "lifecycle != 'archived'"]
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if project:
        where.append("project = ?")
        params.append(project)
    order = "updated_at DESC" if recent else "slug ASC"
    if visible is None:
        sql = f"SELECT * FROM memory_items WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?"
        rows = conn.execute(sql, [*params, limit]).fetchall()
        return [MemoryItem.from_row(r) for r in rows]
    # a filtered listing walks the order on narrow columns and stops at the
    # first `limit` rows the caller may see; full rows are read only for those
    cur = conn.execute(
        f"SELECT id, visibility, topics, agent FROM memory_items "
        f"WHERE {' AND '.join(where)} ORDER BY {order}", params)
    keep: list[int] = []
    for r in cur:
        if visible({"visibility": r["visibility"], "agent": r["agent"],
                    "topics": _parse_json_list(r["topics"])}):
            keep.append(r["id"])
            if len(keep) >= limit:
                cur.close()
                break
    if not keep:
        return []
    fetched = {r["id"]: r for r in conn.execute(
        f"SELECT * FROM memory_items WHERE id IN ({','.join('?' * len(keep))})", keep).fetchall()}
    return [MemoryItem.from_row(fetched[i]) for i in keep if i in fetched]


def _escape_fts(query: str) -> str:
    """Escape a user query for FTS5 MATCH against ``mem_fts_stem``.

    Tokens are Snowball-stemmed then quoted as prefix matches. We use OR
    semantics across tokens (standard search-engine behavior) and rely on
    BM25 to rank documents that match more of them higher. Implicit AND
    would be too strict for natural-language queries — a 5-word question
    almost never has all 5 stems in a single short message.
    """
    # Split the way the FTS5 tokenizer splits the documents, not on whitespace.
    # tool-recall passes a FILE PATH as the query, and on a BM25-only install
    # (`pip install skillmem` without the semantic extra) "/work/analysis.ipynb"
    # became one phrase token and matched nothing at all — recall was silently
    # dead for every Edit/Write/NotebookEdit.
    tokens = _WORD_RE.findall(query)  # same shape as the indexed tokens
    if not tokens:
        return '""'
    parts: list[str] = []
    seen: set[str] = set()
    for raw in tokens:
        stem = _stem_word(raw)
        if len(stem) < 2 or stem in seen:
            continue
        seen.add(stem)
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
    pool: int | None = _CANDIDATE_POOL,
    exclude_kinds: tuple[str, ...] = (),
) -> list[int]:
    """Lexical candidate ids, best-first, from the stemmed FTS5 index.

    ``pool=None`` returns every match (a visibility-filtered caller ranks
    once and takes what it may see)."""
    kind_clause = "AND m.kind = ?" if kind else ""
    project_clause = "AND m.project = ?" if project else ""
    # Excluding kinds AFTER the candidate pool would drop the answer: the pool is
    # capped, so a wall of session recaps can fill it and hide every skill.
    excl_clause = (
        f"AND m.kind NOT IN ({','.join('?' * len(exclude_kinds))})"
        if exclude_kinds else "")
    sql = f"""
        SELECT m.id AS id, bm25(mem_fts_stem) AS r
        FROM mem_fts_stem
        JOIN memory_items m ON m.id = mem_fts_stem.rowid
        WHERE mem_fts_stem MATCH ?
          AND m.deleted_at IS NULL
          AND m.lifecycle != 'archived'
          {kind_clause}
          {project_clause}
          {excl_clause}
        ORDER BY r
        LIMIT ?
    """
    params: list[Any] = [_escape_fts(query)]
    if kind:
        params.append(kind)
    if project:
        params.append(project)
    params.extend(exclude_kinds)
    params.append(-1 if pool is None else pool)
    return [row["id"] for row in conn.execute(sql, params).fetchall()]


def _vector_ids(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    pool: int | None = _CANDIDATE_POOL,
    exclude_kinds: tuple[str, ...] = (),
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
    excl_clause = (
        f"AND kind NOT IN ({','.join('?' * len(exclude_kinds))})"
        if exclude_kinds else "")
    sql = (
        "SELECT id, embedding FROM memory_items "
        "WHERE embedding IS NOT NULL AND deleted_at IS NULL "
        "AND lifecycle != 'archived' "
        f"{kind_clause} {project_clause} {excl_clause}"
    )
    params: list[Any] = []
    if kind:
        params.append(kind)
    if project:
        params.append(project)
    params.extend(exclude_kinds)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return []
    q = np.frombuffer(qb, dtype="float32")
    ids = [r["id"] for r in rows]
    mat = np.stack([np.frombuffer(r["embedding"], dtype="float32") for r in rows])
    sims = mat @ q                       # both sides pre-normalized -> cosine
    order = np.argsort(-sims) if pool is None else np.argsort(-sims)[:pool]
    return [ids[int(i)] for i in order if float(sims[int(i)]) >= _MIN_COSINE]


def _keep_visible(
    conn: sqlite3.Connection,
    ids: list[int],
    visible: Callable[[dict[str, Any]], bool] | None,
    limit: int,
) -> list[int]:
    """The first ``limit`` of ``ids`` (rank order kept) that ``visible`` accepts.

    One narrow query per 500 ids — visibility, topics and agent only — so a
    caller who may see nothing costs one ranking pass and no body reads. The
    HTTP layer used to widen its page 5→20→80→… re-ranking and re-fetching
    full rows each time; with 9k hidden rows that was ~16k rows of bodies.
    """
    if visible is None:
        return ids[:limit]
    out: list[int] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            f"SELECT id, visibility, topics, agent FROM memory_items "
            f"WHERE id IN ({','.join('?' * len(chunk))})", chunk,
        ).fetchall()
        meta = {r["id"]: {"visibility": r["visibility"], "agent": r["agent"],
                          "topics": _parse_json_list(r["topics"])} for r in rows}
        for i in chunk:
            m = meta.get(i)
            if m is not None and visible(m):
                out.append(i)
                if len(out) >= limit:
                    return out
    return out


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
    exclude_kinds: tuple[str, ...] = (),
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[int]:
    """Fused id ranking. Falls back to pure BM25 when no vector signal.

    With ``visible`` every match is ranked once and the first ``limit`` ids
    the predicate accepts are returned — a fixed candidate pool let hidden
    rows crowd a caller's own record out of the page entirely.
    """
    # unfiltered callers keep the fixed pool: RRF over a pool that grows with
    # the limit is not prefix-stable (top-5 at limit 5 != top-5 at limit 100)
    pool = None if visible is not None else _CANDIDATE_POOL
    bm = _bm25_ids(conn, query, kind=kind, project=project,
                   exclude_kinds=exclude_kinds, pool=pool)
    vec = _vector_ids(conn, query, kind=kind, project=project,
                      exclude_kinds=exclude_kinds, pool=pool)
    if not vec:
        return _keep_visible(conn, bm, visible, limit)
    scores = _rrf_scores(bm, vec)
    ordered = sorted(scores, key=lambda i: -scores[i])
    return _keep_visible(conn, ordered, visible, limit)


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 10,
    exclude_kinds: tuple[str, ...] = (),
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    if kind:
        try:
            kind = _valid_kind(kind)  # "Reference" filters find "reference" rows
        except ValueError:
            return []                 # a filter nothing can match matches nothing
    ids = hybrid_rank_ids(conn, query, kind=kind, project=project, limit=limit,
                          exclude_kinds=exclude_kinds, visible=visible)
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
        # Raw float32 blob: garbage in CLI --format json and a serialization
        # 500 in the HTTP layer. Nothing downstream reads it from a hit.
        d.pop("embedding", None)
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
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Return existing memories whose word content overlaps the new one.

    ``visible`` gets ``{"visibility", "topics", "agent"}`` of each candidate
    and drops the ones the caller may not see: a 409 that names another
    agent's private title is a read through the trust boundary.

    Uses FTS5 BM25 to surface candidates (cheap), then computes asymmetric
    inclusion overlap ``|A ∩ B| / min(|A|, |B|)`` on the bag of words: if 70%
    of one doc's words are in the other, treat it as a duplicate — regardless
    of length. No LLM involved.
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
        # The visibility filter runs in Python, so with a filter the SQL has no
        # LIMIT: the cursor walks the BM25 order and stops once `candidates`
        # VISIBLE rows are scored. A fixed window (5, then 100) let that many
        # hidden rows crowd out the writer's own duplicate.
        # Narrow on purpose: without a LIMIT SQLite sorts every match before
        # the first row comes out, and dragging `body` through that sort cost
        # 0.3-0.5 s per write on a 9k-row database. Bodies are fetched below
        # for the few rows that get scored.
        rows = conn.execute(
            "SELECT m.id, m.slug, m.visibility, m.topics, m.agent "
            "FROM mem_fts_stem "
            "JOIN memory_items m ON m.id = mem_fts_stem.rowid "
            "WHERE mem_fts_stem MATCH ? AND m.deleted_at IS NULL "
            "ORDER BY bm25(mem_fts_stem) LIMIT ?",
            (fts_query, candidates if visible is None else -1),
        )
    except sqlite3.OperationalError as exc:
        # Log so a broken FTS index isn't silently treated as "no conflicts".
        log.warning("find_conflicts FTS query failed (%s); treating as empty", exc)
        return []

    conflicts: list[dict[str, Any]] = []
    scored = 0
    for row in rows:
        if exclude_slug and row["slug"] == exclude_slug:
            continue
        if visible is not None and not visible({
            "visibility": row["visibility"], "agent": row["agent"],
            "topics": _parse_json_list(row["topics"]),
        }):
            continue
        if scored >= candidates:          # the top-N *visible* by BM25, as before the filter
            rows.close()
            break
        scored += 1
        text = conn.execute(
            "SELECT title, body FROM memory_items WHERE id = ?", (row["id"],)
        ).fetchone()
        if text is None:                  # deleted between the walk and now
            continue
        other = _word_bag(text["title"] + "\n" + text["body"])
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
                "title": text["title"],
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
    unapproved = 0

    ordered = [k for k in _INJECT_KIND_ORDER if k in kinds] + [
        k for k in kinds if k not in _INJECT_KIND_ORDER
    ]

    for kind in ordered:
        # Titles only, and only what the owner approved: the briefing has no room
        # for a frame, and a title is text from the same source as its body —
        # an unapproved one would arrive looking like a rule the owner set.
        rows = conn.execute(
            """
            SELECT slug, title, updated_at FROM memory_items
            WHERE kind = ? AND deleted_at IS NULL AND trusted_at IS NOT NULL
              AND lifecycle != 'archived'
            ORDER BY updated_at DESC LIMIT ?
            """,
            (kind, per_kind_limit),
        ).fetchall()
        unapproved += conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE kind = ? AND deleted_at IS NULL "
            "AND trusted_at IS NULL", (kind,),
        ).fetchone()[0]
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
        "unapproved": unapproved,
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
    fts_count = conn.execute("SELECT COUNT(*) AS n FROM mem_fts_stem").fetchone()["n"]
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

def skill_body(trigger: str, steps: str, outcome: str, lessons: str | None = None) -> str:
    """Canonical body for a learned skill — single source for CLI/MCP/HTTP."""
    parts = [
        f"**trigger:** {trigger}",
        f"**steps:** {steps}",
        f"**outcome:** {outcome}",
    ]
    if lessons:
        parts.append(f"**lessons:** {lessons}")
    return "\n".join(parts)


STRENGTH_BOOST = 0.15
STRENGTH_CAP = 2.0
DECAY_FACTOR = 0.85
DECAY_FLOOR = 0.05
# Lifecycle thresholds: active -> stale -> archived. Archived
# skills are excluded from recall but never deleted — restorable one command.
STALE_AFTER_DAYS = 30
ARCHIVE_AFTER_DAYS = 90


#: What counts as evidence that a skill helped, and what it does to strength.
#: Retrieval is not evidence: an agent that recalls its own skill and declares
#: it useful would otherwise reinforce its own mistake, and a wrong skill that
#: keeps getting recalled would outrank a right one nobody needed lately.
#: Only a signal from outside the agent's own judgement moves strength up.
EVIDENCE_WEIGHTS: dict[str, float] = {
    "self_report": 0.0,      # the agent says it helped — recorded, not rewarded
    "test_passed": STRENGTH_BOOST,
    "diff_accepted": STRENGTH_BOOST,
    "user_confirmed": STRENGTH_BOOST,
    "failure": 0.0,          # handled separately: multiplies strength down
}
#: A skill followed by a failure loses ground faster than idleness takes it.
FAILURE_FACTOR = 0.7


def reinforce(
    conn: sqlite3.Connection,
    slug: str,
    *,
    evidence: str = "self_report",
) -> dict[str, Any] | None:
    """Record that a skill was used, and move its strength by the evidence.

    ``evidence`` is one of ``EVIDENCE_WEIGHTS``. ``self_report`` (the default,
    and what plain retrieval produces) refreshes recency and the access count
    but leaves strength alone. ``test_passed`` / ``diff_accepted`` /
    ``user_confirmed`` are outside signals and raise it. ``failure`` says the
    task went wrong after the skill was applied and lowers it.
    """
    if evidence not in EVIDENCE_WEIGHTS:
        raise ValueError(
            f"unknown evidence {evidence!r}; expected one of "
            f"{', '.join(sorted(EVIDENCE_WEIGHTS))}"
        )
    # skills only: strength and decay are a skill's mechanics, and every
    # channel's description promises a non-skill is refused
    row = conn.execute(
        "SELECT id, strength, access_count, confirmed_count, failure_count "
        "FROM memory_items WHERE slug = ? AND deleted_at IS NULL AND kind = 'skill'",
        (slug,),
    ).fetchone()
    if not row:
        return None

    now = _now()
    # One statement, relative arithmetic: two confirmations landing together
    # used to read the same counters and one overwrote the other.
    if evidence == "failure":
        conn.execute(
            "UPDATE memory_items SET strength = MAX(?, strength * ?), "
            "access_count = access_count + 1, last_accessed_at = ?, "
            "failure_count = failure_count + 1 WHERE id = ?",
            (DECAY_FLOOR, FAILURE_FACTOR, now, row["id"]),
        )
    else:
        boost = EVIDENCE_WEIGHTS[evidence]
        conn.execute(
            "UPDATE memory_items SET strength = MIN(?, strength + ?), "
            "access_count = access_count + 1, last_accessed_at = ?, "
            "confirmed_count = confirmed_count + ? WHERE id = ?",
            (STRENGTH_CAP, boost, now, 1 if boost > 0 else 0, row["id"]),
        )
    fresh = conn.execute(
        "SELECT strength, access_count, confirmed_count, failure_count "
        "FROM memory_items WHERE id = ?", (row["id"],),
    ).fetchone()
    new_strength, new_count = fresh["strength"], fresh["access_count"]
    confirmed, failures = fresh["confirmed_count"], fresh["failure_count"]
    return {"slug": slug, "strength": round(new_strength, 3),
            "access_count": new_count, "evidence": evidence,
            "confirmed_count": confirmed, "failure_count": failures}


def set_pinned(
    conn: sqlite3.Connection, slug: str, pinned: bool
) -> dict[str, Any] | None:
    """Pin or unpin a skill. A pinned skill never decays and is never archived.

    For the rule that matters *because* it is rarely needed — "deploy only
    through the gate", "never force-push to main" — rarity is the whole point,
    and decay would read it as irrelevance.
    """
    row = conn.execute(
        "SELECT id, pinned, lifecycle FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row:
        return None
    # the flag only — updated_at is the text's age, and pinning is not an edit,
    # and lifecycle is not pinning's business: an archived row comes back
    # through the one call that says so (set_archived / mem_archive), so that
    # strength is never handed out by a side effect of a different verb.
    conn.execute(
        "UPDATE memory_items SET pinned = ? WHERE id = ?",
        (1 if pinned else 0, row["id"]),
    )
    return {"slug": slug, "pinned": pinned, "changed": bool(row["pinned"]) != pinned,
            "lifecycle": row["lifecycle"]}


def decay_stale(
    conn: sqlite3.Connection,
    *,
    days_threshold: int = 14,
    kind: str = "skill",
) -> list[dict[str, Any]]:
    """Ebbinghaus decay: reduce strength of skills not accessed recently."""
    now = _now()
    days_threshold = max(1, int(days_threshold))   # 0 or negative compounded on every run
    cutoff = now - days_threshold * 86400
    # A skill nobody has recalled yet is measured from its birth, not from
    # "never" — otherwise the first nightly run hits a day-old skill. And one
    # decay step per elapsed threshold: running the job twice in a night, or
    # by hand after it, used to compound 0.85 each time.
    rows = conn.execute(
        "SELECT id, slug, strength FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND strength > ? AND pinned = 0 "
        "AND COALESCE(last_accessed_at, created_at) <= ? "
        "AND COALESCE(last_decayed_at, 0) <= ?",
        (kind, DECAY_FLOOR, cutoff, cutoff),
    ).fetchall()
    decayed: list[dict[str, Any]] = []
    for r in rows:
        new_strength = max(DECAY_FLOOR, r["strength"] * DECAY_FACTOR)
        conn.execute(
            "UPDATE memory_items SET strength = ?, last_decayed_at = ? WHERE id = ?",
            (new_strength, now, r["id"]),
        )
        decayed.append({
            "slug": r["slug"],
            "old_strength": round(r["strength"], 3),
            "new_strength": round(new_strength, 3),
        })
    return decayed


def _backup_skills(rows: list[sqlite3.Row], reason: str) -> None:
    """Append a human-readable JSONL snapshot before archiving (pre-prune
    backup). Best-effort: a write failure never blocks archiving."""
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
    Pinned skills sit out both transitions: they stay active however long they
    go unused, which is the point of pinning them.
    Archived skills are excluded from recall (see _bm25_ids/_vector_ids).
    """
    now = _now()
    stale_cut = now - STALE_AFTER_DAYS * 86400
    archive_cut = now - ARCHIVE_AFTER_DAYS * 86400

    # COALESCE: a never-recalled skill counts idle time from its creation.
    archive_rows = conn.execute(
        "SELECT id, slug, title, body, strength, last_accessed_at FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND lifecycle != 'archived' "
        # owner_seal, like pinned: the nightly job is the slow path to the same
        # place mem_archive is refused, and mem_reinforce evidence='failure'
        # lets an agent walk a record's strength down to the floor on purpose.
        "AND pinned = 0 AND owner_seal = 0 "
        "AND strength <= ? AND COALESCE(last_accessed_at, created_at) < ?",
        (kind, DECAY_FLOOR, archive_cut),
    ).fetchall()
    _backup_skills(archive_rows, "archive")
    archived = [r["slug"] for r in archive_rows]
    for r in archive_rows:
        # the same audit row an explicit archive leaves: "gone from every read"
        # must be answerable afterwards however it happened
        _append_lifecycle_history(conn, r["slug"], r, "archived by nightly sweep", "sweep")
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'archived' WHERE id = ?", (r["id"],)
        )

    stale_rows = conn.execute(
        "SELECT id, slug FROM memory_items "
        "WHERE kind = ? AND deleted_at IS NULL AND lifecycle = 'active' "
        # 'stale' still shows up in every read, so it needs no seal exemption
        "AND pinned = 0 "
        "AND COALESCE(last_accessed_at, created_at) < ?",
        (kind, stale_cut),
    ).fetchall()
    staled = [r["slug"] for r in stale_rows]
    for r in stale_rows:
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'stale' WHERE id = ?", (r["id"],)
        )
    # No commit: the connection is autocommit (isolation_level=None), so the
    # UPDATEs above are already durable. An explicit commit() here would close
    # a caller's open `with tx()` block early and break SAVEPOINT nesting.
    return {"staled": staled, "archived": archived}


def lifecycle_counts(
    conn: sqlite3.Connection, *, kind: str | None = None
) -> dict[str, int]:
    """Count records per lifecycle state. kind=None counts every kind: an agent
    can archive a note or a feedback rule too, and a skills-only count made
    those invisible to the one command that reports the lifecycle."""
    rows = conn.execute(
        "SELECT lifecycle, COUNT(*) c FROM memory_items "
        "WHERE (? IS NULL OR kind = ?) AND deleted_at IS NULL GROUP BY lifecycle",
        (kind, kind),
    ).fetchall()
    return {r["lifecycle"]: r["c"] for r in rows}


def _append_lifecycle_history(
    conn: sqlite3.Connection, slug: str, row: Any, reason: str, by: str | None
) -> None:
    """One tamper-evident row per lifecycle change — the owner's only trace of it."""
    now = _chain_clock(conn, _now())
    prev_hash = _last_chain_hash(conn)
    payload = {
        "slug": slug, "old_title": row["title"], "old_body": row["body"],
        "changed_at": now, "changed_by": by, "reason": reason,
    }
    conn.execute(
        "INSERT INTO memory_history (slug, old_title, old_body, changed_at, changed_by,"
        " reason, prev_hash, self_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (slug, row["title"], row["body"], now, by, reason,
         prev_hash, _chain_hash(prev_hash, payload)),
    )


def set_archived(
    conn: sqlite3.Connection, slug: str, archived: bool = True, *, by: str | None = None
) -> dict[str, Any] | None:
    """Archive a record (out of search, recall and inject; kept, reversible)
    or bring it back. A pinned record is refused — pin means "never archive".

    Deletion stays with the owner at the CLI; an agent gets to say "this no
    longer applies" without erasing anything.
    """
    row = conn.execute(
        "SELECT id, pinned, lifecycle, title, body FROM memory_items "
        "WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    if not row:
        return None
    if archived and row["pinned"]:
        raise ValueError(f"'{slug}' is pinned; unpin it before archiving it")
    # Hiding a record from every read is a change the owner must be able to see
    # afterwards: mem_update leaves a history row, and so does this. The text is
    # untouched, so the row records what was hidden, not a new version.
    moving = (row["lifecycle"] != "archived") if archived else (row["lifecycle"] != "active")
    if moving:
        # only a real transition gets a row: "restored from active" recorded a
        # change that never happened, and an agent could repeat it at will
        _append_lifecycle_history(
            conn, slug, row,
            "archived" if archived else f"restored from {row['lifecycle']}",
            by,
        )
    if archived:
        # lifecycle only — updated_at is the text's age, and archiving is not
        # an edit; touching it would reorder listings and reset freshness
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'archived' WHERE id = ?", (row["id"],)
        )
    elif row["lifecycle"] != "active":
        # only a real restore refreshes recency and floors strength, or the
        # nightly sweep_lifecycle would archive it again on its next run;
        # calling this on an active row must not hand out strength for free
        conn.execute(
            "UPDATE memory_items SET lifecycle = 'active', "
            "strength = MAX(strength, ?), last_accessed_at = ? WHERE id = ?",
            (0.5, _now(), row["id"]),
        )
    return {"slug": slug, "lifecycle": "archived" if archived else "active",
            "was": row["lifecycle"]}


def restore_skill(conn: sqlite3.Connection, slug: str, *, by: str | None = None) -> bool:
    """Bring a hidden skill back to 'active'. One implementation, shared with
    set_archived(archived=False): the two used to hold the same UPDATE, and only
    one of them learned not to hand strength to a row that was never hidden."""
    return set_archived(conn, slug, False, by=by) is not None


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


def recall_skills(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 5,
    auto_reinforce: bool = True,
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Find relevant skills for a task, optionally reinforcing them.

    Hybrid BM25+vector fusion (RRF), then a gentle strength bonus
    (``SKILL_STRENGTH_COEF``) so frequently-useful skills surface higher.
    Degrades to BM25-only when no embeddings are present (see hybrid_rank_ids).
    """
    pool = None if visible is not None else _CANDIDATE_POOL
    bm = _bm25_ids(conn, query, kind="skill", pool=pool)
    vec = _vector_ids(conn, query, kind="skill", pool=pool)
    fused = _rrf_scores(bm, vec) if vec else {i: 1.0 / (RRF_K + r + 1) for r, i in enumerate(bm)}
    if not fused:
        return []
    # apply strength bonus, then take top `limit`
    strength_by_id: dict[int, float] = {}
    fused_ids = list(fused)
    for start in range(0, len(fused_ids), 500):   # a filtered call fuses every match
        chunk = fused_ids[start:start + 500]
        strength_by_id.update({
            row["id"]: row["strength"]
            for row in conn.execute(
                f"SELECT id, strength FROM memory_items WHERE id IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
        })
    ranked_ids = _keep_visible(conn, sorted(
        fused, key=lambda i: -fused[i] * (1.0 + strength_by_id.get(i, 0.0) * SKILL_STRENGTH_COEF)
    ), visible, limit)
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
            # Provenance and approval. Omitting them made every skill — including
            # the ones the owner had approved — read as unapproved downstream,
            # which is how a trust marker stops meaning anything.
            "origin": (row["origin"] if "origin" in row.keys() else "unknown"),
            "trusted_at": (row["trusted_at"] if "trusted_at" in row.keys() else None),
            "kind": row["kind"],
            "tags": _parse_json_list(row["tags"]),
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
