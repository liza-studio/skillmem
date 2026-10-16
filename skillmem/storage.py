"""SQLite + FTS5 storage layer for skillmem.

Full record provenance (birth certificate / supersession / death record)
is built in:
- created_at/updated_at and source_session = birth certificate
- ttl_days + freshness_until = expiration
- memory_history table = death record

FTS5 mirrors title+body via triggers so writes stay simple.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
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
# A redaction marker is not a secret: scrubbing a scrubbed body is a no-op, or
# its hash (and approval) changed on every re-write.
_SECRET_ASSIGN = _re.compile(
    r"""(?i)\b(password|passwd|pwd|secret|api[_\-]?key|token|access[_\-]?token)\b"""
    r"""(\s*[:=]\s*)(["']?)(?!\[[a-z\-]+ redacted\])([^\s"',;]{6,})(["']?)""",
)
_WIKILINK = _re.compile(r"\[\[([^\]\n]+?)\]\]")


def _absolute(path: str) -> Path:
    """An override made absolute: a relative one written into a scheduled
    job meant another database from the job's working directory (INV-12)."""
    try:
        return Path(path).expanduser().absolute()
    except OSError:   # a vanished cwd: opening reports it
        return Path(path).expanduser()


def default_data_dir() -> Path:
    override = os.environ.get("SKILLMEM_HOME")
    if override:
        return _absolute(override)
    return Path(user_data_dir(APP_NAME, appauthor=False))


def default_db_path() -> Path:
    """``SKILLMEM_DB`` wins over ``SKILLMEM_HOME`` wins over OS user-data."""
    db_override = os.environ.get("SKILLMEM_DB")
    if db_override:
        return _absolute(db_override)
    return default_data_dir() / "memory.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection in autocommit mode.

    Multi-statement writes (upsert + history + body file) must be wrapped
    in :func:`tx` so they commit atomically. Anything else just runs in
    autocommit — keeps callers simple, no need to remember `.commit()`.
    """
    path = Path(db_path).expanduser() if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # concurrent writers wait for the lock instead of failing at once
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

# Embeddings asked for inside a transaction, by connection: computed after the
# outermost COMMIT, so the model never runs under the write lock.
_deferred_embeddings: dict[int, list[tuple[Any, ...]]] = {}


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic write block (BEGIN IMMEDIATE), re-entrant via SAVEPOINTs.
    Embeddings requested inside it are computed after the outermost COMMIT,
    so the model never runs under the write lock (INV-09)."""
    if conn.in_transaction:
        sp = "sm_sp_" + _uuid.uuid4().hex[:12]  # guaranteed unique name
        conn.execute(f"SAVEPOINT {sp}")
        try:
            yield conn
            conn.execute(f"RELEASE {sp}")
        except BaseException:
            conn.execute(f"ROLLBACK TO {sp}")
            conn.execute(f"RELEASE {sp}")
            raise
        return

    try:
        # inside the try: a Ctrl-C while BEGIN waits for the lock is raised as
        # it returns, and left the transaction open under every later write
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        # COMMIT too (r16 review): one that failed kept the lock open
        conn.execute("COMMIT")
        for args in _deferred_embeddings.pop(id(conn), []):
            _set_embedding(conn, *args)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        _deferred_embeddings.pop(id(conn), None)
        raise


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Several reads that see one committed state. A deferred BEGIN: in WAL
    mode it neither takes nor waits for the write lock, so a long writer
    elsewhere does not stall it (tx() would, for the whole busy timeout)."""
    if conn.in_transaction:
        yield conn
        return
    try:
        conn.execute("BEGIN")     # inside the try, as in tx()
        yield conn
    finally:
        try:
            if conn.in_transaction:
                conn.execute("COMMIT")
        finally:
            if conn.in_transaction:     # the COMMIT failed
                conn.execute("ROLLBACK")


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
    supersedes_id   INTEGER REFERENCES memory_items(id) ON DELETE SET NULL, -- unused
    confidence      REAL NOT NULL DEFAULT 1.0,     -- unused: no surface sets or reads it
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


CURRENT_SCHEMA_VERSION = 12


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotent schema bootstrap. Cheap on every call after first run."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Once per database, in one statement: a new one gets its id, one that
    # already holds records keeps naming its files by path ("", _db_identity).
    if conn.execute("SELECT 1 FROM meta WHERE key = 'db_id'").fetchone() is None:
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) SELECT 'db_id', CASE WHEN "
            "EXISTS (SELECT 1 FROM memory_items) THEN '' ELSE ? END",
            (_uuid.uuid4().hex,))
    # ... and the file it was given in: a byte copy carries the id (INV-12)
    if _db_inode(conn) and conn.execute(
            "SELECT 1 FROM meta WHERE key = 'db_file'").fetchone() is None:
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('db_file', ?)",
                     (_db_inode(conn),))
    _adopt_body_files(conn)
    # An open takes no write lock unless there is something to change: a hook
    # opening behind any writer would stall for the whole busy_timeout.


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


def _add_column(conn: sqlite3.Connection, ddl: str, table: str = "memory_items") -> None:
    """ALTER ... ADD COLUMN that tolerates losing the race to another opener."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except sqlite3.OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise


def _migrate(conn: sqlite3.Connection) -> None:
    """Forward-only schema patches. Backfills run once, then are skipped."""
    if _pre_v10(conn):
        # the backup is the database before any repair, under the repairs' lock (INV-12)
        with tx(conn):
            if _pre_v10(conn):
                _backup_before_v10(conn)
            _heal(conn)
    else:
        _heal(conn)

    if _current_schema_version(conn) >= CURRENT_SCHEMA_VERSION:
        return
    # one transaction, the version asked again under its lock (INV-05)
    with tx(conn):
        if _current_schema_version(conn) < CURRENT_SCHEMA_VERSION:
            _migrate_versioned(conn)


def _pre_v10(conn: sqlite3.Connection) -> bool:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    return not {name for name, _ in _V10_COLUMNS} <= have


def _heal(conn: sqlite3.Connection) -> None:
    """The unversioned steps, checked on every open. Each reads first, so the
    common case takes no write lock, and each UPDATE's WHERE re-states the read
    (INV-05). A stored value upsert would refuse is repaired to the nearest one
    it accepts, or the record's own dump would not restore (INV-06)."""
    # columns guaranteed whatever the version says: an interrupted migration
    # could strand a version without its ALTER
    live_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "embedding" not in live_cols:
        _add_column(conn, "embedding BLOB")
    if "lifecycle" not in live_cols:
        _add_column(conn, "lifecycle TEXT NOT NULL DEFAULT 'active'")
    if "last_decayed_at" not in live_cols:
        _add_column(conn, "last_decayed_at INTEGER")
    # kinds: every one with a character or a length _KIND_RE refuses, or a space to collapse
    odd = conn.execute(
        "SELECT id, kind FROM memory_items WHERE kind GLOB '*[^a-z0-9_ -]*' "
        "OR length(kind) NOT BETWEEN 1 AND 32 OR kind != TRIM(kind) OR kind LIKE '%  %'"
    ).fetchall()
    # approval is bound to the kind (INV-01): `Feedback` was no rule, `feedback` is
    unapprove = ", trusted_at = NULL, trusted_by = NULL" if "trusted_at" in live_cols else ""
    for r in odd:
        norm = _repaired_kind(r["kind"])
        if norm != r["kind"]:
            conn.execute(f"UPDATE memory_items SET kind = ?{unapprove} WHERE id = ? AND kind = ?",
                         (norm, r["id"], r["kind"]))
    # visibility: an off-enum value ("team", "../x") is private
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
    # strength and TTL out of range
    if conn.execute("SELECT 1 FROM memory_items WHERE strength NOT BETWEEN 0 AND ? "
                    "OR typeof(ttl_days) NOT IN ('integer', 'null') "
                    "OR ttl_days NOT BETWEEN 1 AND 3650 LIMIT 1",
                    (STRENGTH_CAP,)).fetchone() is not None:
        conn.execute("UPDATE memory_items SET strength = MIN(MAX(strength, 0), ?) "
                     "WHERE strength NOT BETWEEN 0 AND ?", (STRENGTH_CAP, STRENGTH_CAP))
        conn.execute("UPDATE memory_items SET ttl_days = CASE WHEN CAST(ttl_days AS INTEGER) < 1 "
                     "THEN NULL ELSE MIN(CAST(ttl_days AS INTEGER), 3650) END "
                     "WHERE typeof(ttl_days) NOT IN ('integer', 'null') "
                     "OR ttl_days NOT BETWEEN 1 AND 3650")
    # "" (0.11.3 `write --project ""`) is the NULL a write now stores: a dump carries only null
    for name in ("project", "agent", "source_session"):
        if conn.execute(f"SELECT 1 FROM memory_items WHERE {name} = '' LIMIT 1").fetchone():
            conn.execute(f"UPDATE memory_items SET {name} = NULL WHERE {name} = ''")
    if not {"origin", "trusted_at", "trusted_by"} <= live_cols:
        _migrate_v10(conn)
    _migrate_owner_seal(conn)     # after v10: its backfill reads origin and trusted_at


def _migrate_versioned(conn: sqlite3.Connection) -> None:
    """The version-gated steps; the caller holds the write lock."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "body_path" not in cols:
        _add_column(conn, "body_path TEXT")
    if "stemmed" not in cols:
        _add_column(conn, "stemmed TEXT NOT NULL DEFAULT ''")

    history_cols = {row["name"] for row in conn.execute("PRAGMA table_info(memory_history)")}
    if "prev_hash" not in history_cols:
        _add_column(conn, "prev_hash TEXT", table="memory_history")
    if "self_hash" not in history_cols:
        # The column and the chain land together, so only a database that never
        # had the column is signed: one with its hashes cleared is a tamper.
        _add_column(conn, "self_hash TEXT", table="memory_history")
        _backfill_history_chain(conn)

    # The index and its triggers are built whole or rebuilt, statement by
    # statement (executescript() commits first): a table without its triggers
    # is an index no write maintains.
    build = {"mem_fts_stem", "mem_stem_ai", "mem_stem_ad", "mem_stem_au"} - {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if build:
        for name in ("mem_stem_ai", "mem_stem_ad", "mem_stem_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts_stem USING fts5(
                stemmed,
                content='memory_items', content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            )""")

    rows = conn.execute(
        "SELECT * FROM memory_items WHERE stemmed IS NULL OR stemmed = ''"
    ).fetchall()
    if rows:
        log.info("backfilling Snowball stems for %d rows", len(rows))
        for r in rows:
            conn.execute("UPDATE memory_items SET stemmed = ? WHERE id = ?",
                         (_row_stems(r), r["id"]))

    if build:
        conn.execute("INSERT INTO mem_fts_stem(mem_fts_stem) VALUES('rebuild')")
        conn.execute("""
            CREATE TRIGGER mem_stem_ai AFTER INSERT ON memory_items BEGIN
                INSERT INTO mem_fts_stem(rowid, stemmed) VALUES (new.id, new.stemmed);
            END""")
        conn.execute("""
            CREATE TRIGGER mem_stem_ad AFTER DELETE ON memory_items BEGIN
                INSERT INTO mem_fts_stem(mem_fts_stem, rowid, stemmed)
                VALUES('delete', old.id, old.stemmed);
            END""")
        conn.execute("""
            CREATE TRIGGER mem_stem_au AFTER UPDATE ON memory_items BEGIN
                INSERT INTO mem_fts_stem(mem_fts_stem, rowid, stemmed)
                VALUES('delete', old.id, old.stemmed);
                INSERT INTO mem_fts_stem(rowid, stemmed) VALUES (new.id, new.stemmed);
            END""")

    # v5: skill learning columns
    if "access_count" not in cols:
        _add_column(conn, "access_count INTEGER NOT NULL DEFAULT 0")
    if "last_accessed_at" not in cols:
        _add_column(conn, "last_accessed_at INTEGER")

    # v8: drop the legacy porter FTS index, superseded by mem_fts_stem
    for ddl in ("DROP TRIGGER IF EXISTS memory_items_ai", "DROP TRIGGER IF EXISTS memory_items_ad",
                "DROP TRIGGER IF EXISTS memory_items_au", "DROP TABLE IF EXISTS mem_fts"):
        conn.execute(ddl)

    # v9: pinning and evidence counters
    if "pinned" not in cols:
        _add_column(conn, "pinned INTEGER NOT NULL DEFAULT 0")
    if "confirmed_count" not in cols:
        _add_column(conn, "confirmed_count INTEGER NOT NULL DEFAULT 0")
    if "failure_count" not in cols:
        _add_column(conn, "failure_count INTEGER NOT NULL DEFAULT 0")

    # v11: two-character tokens joined the lexical index. Rebuilding it takes
    # about a minute on a large database, too long for a hook's open, so only a
    # non-empty database is flagged: the nightly decay job or
    # `skillmem reindex-lexical` rebuilds it.
    if _current_schema_version(conn) < 11 and conn.execute(
            "SELECT EXISTS(SELECT 1 FROM memory_items)").fetchone()[0]:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES "
            "('lexical_reindex_pending', '1')")

    # v12: each body where a write puts it (_body_in_file). Before, a file was
    # kept on the same text and a document from before body files was inline,
    # so get().body was not what the record's own dump restored (INV-06). A
    # repair: the text, its hash and updated_at stay (INV-03).
    if _current_schema_version(conn) < 12:
        for r in conn.execute("SELECT id, slug, kind, title, body, body_path, content_hash "
                              "FROM memory_items").fetchall():
            text = (verified_body_file(r["title"], r["body_path"], r["content_hash"])
                    if r["body_path"] else r["body"])
            if text is None or _body_in_file(r["kind"], text) == bool(r["body_path"]):
                continue    # an unverified file is not the text, and stays
            path = None
            if not r["body_path"]:
                try:
                    _stage_body_file(conn, r["slug"], text, r["content_hash"])
                except OSError:
                    continue    # an open never fails over a placement
                path = _body_filename(r["slug"], ns=_db_namespace(conn),
                                      content_hash=r["content_hash"])
            conn.execute("UPDATE memory_items SET body = ?, body_path = ? WHERE id = ?",
                         (_make_excerpt(text) if path else text, path, r["id"]))

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION),),
    )


def lexical_reindex_pending(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'lexical_reindex_pending'").fetchone()
    return bool(row and str(row["value"]) == "1")


def restem_all(conn: sqlite3.Connection) -> int:
    """Rebuild the lexical index and clear the pending flag. Minutes, not seconds,
    on a large database — call it from a scheduled job or by hand, never from a
    hook."""
    n = 0
    with tx(conn):     # read under the lock: an edit committed before it is indexed
        for r in conn.execute("SELECT * FROM memory_items").fetchall():
            conn.execute("UPDATE memory_items SET stemmed = ? WHERE id = ?",
                         (_row_stems(r), r["id"]))
            n += 1
    log.info("re-stemmed %d rows for v11", n)
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES "
                 "('lexical_reindex_pending', '0')")
    return n


def _row_stems(r: sqlite3.Row) -> str:
    """A stored row's lexical index text: what upsert writes for it — title,
    whole body, tags and topics. The v5 backfill stemmed title and excerpt only."""
    # the DB body is only an excerpt for externalized documents; index
    # the whole text or the tail stops matching after every reindex
    body = load_body(MemoryItem.from_row(r)) if r["body_path"] else r["body"]
    return _stem_text(f"{r['title']}\n{body}\n"
                      + " ".join(_parse_json_list(r["tags"]) + _parse_json_list(r["topics"])))


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

    SQLite's backup API, so a WAL in flight cannot produce a torn copy; a failure
    never blocks the (additive) upgrade. Named for the database and the second
    (INV-12), written beside its name and renamed into place (INV-16). Called
    under the migration's write lock, after the check that the database is
    unmigrated (INV-05), and so copied through a second connection: the backup
    API waits forever on a source its own connection holds that lock on.
    """
    tmp = None
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not path:
            return  # :memory:
        dest = Path(path).parent / "backups" / f"pre-v10-{Path(path).name}-{int(time.time())}.db"
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
        source, out = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True), sqlite3.connect(str(tmp))
        try:
            source.backup(out)
        finally:
            source.close()
            out.close()
        os.replace(tmp, dest)
        log.info("pre-v10 backup written to %s", dest)
    except Exception as exc:  # noqa: BLE001 - never block the upgrade
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        log.warning("could not write the pre-v10 backup: %s", exc)


def _migrate_owner_seal(conn: sqlite3.Connection) -> None:
    """Add owner_seal and seal what the owner already wrote or approved.

    Set once, never cleared (INV-02): `origin` and `trusted_at` both move under an
    agent's own writes, so neither can carry a rule the agent must not lift.
    Checked on every open, like v10's columns. The backfill is one-shot, recorded
    in `meta`: once it has run, upsert is the only place that mints the seal, and
    a later open never seals a row an agent wrote with origin='owner'.
    """
    # read first: no write lock on an open with nothing to do
    have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
    if "owner_seal" in have and conn.execute(
            "SELECT 1 FROM meta WHERE key = 'owner_seal_backfill_done'").fetchone():
        return
    with tx(conn):
        have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
        if "owner_seal" not in have:
            _add_column(conn, "owner_seal INTEGER NOT NULL DEFAULT 0")
            # a marker without its column (an interrupted migration): the
            # column came back empty, so the backfill runs again
            conn.execute("DELETE FROM meta WHERE key = 'owner_seal_backfill_done'")
        if conn.execute("SELECT 1 FROM meta WHERE key = 'owner_seal_backfill_done'"
                        ).fetchone() is None:
            conn.execute(
                "UPDATE memory_items SET owner_seal = 1 "
                "WHERE owner_seal = 0 AND (origin = 'owner' OR trusted_at IS NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES "
                "('owner_seal_backfill_done', '1')"
            )


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Add provenance + approval, atomically, once: the columns are checked
    again under the write lock another opener may hold."""
    with tx(conn):
        have = {row["name"] for row in conn.execute("PRAGMA table_info(memory_items)")}
        missing = [(name, decl) for name, decl in _V10_COLUMNS if name not in have]
        for name, decl in missing:
            _add_column(conn, f"{name} {decl}")
        if missing:
            _backfill_origin(conn)


def _classify_tags(conn: sqlite3.Connection) -> tuple[list[int], list[int]]:
    """Split rows into (imported, unreadable) by their tag list, in Python:
    one malformed list must not fail the rule and let a pack fall through to the
    kind rules. A row whose tags will not parse stays `unknown`, never
    grandfathered: truncated JSON can hide the marker (`'["imported",'`)."""
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
    """Label existing rows, then grandfather only what the owner accumulated:
    their own notes and rules, and the skills their sessions learned (INV-02's
    stated exception). Never an imported pack, a transcript summary (`derived`)
    or a row whose tags cannot be read."""
    now = int(time.time())
    imported, unreadable = _classify_tags(conn)
    if imported:
        marks = ",".join("?" * len(imported))
        conn.execute(
            f"UPDATE memory_items SET origin = 'imported' WHERE id IN ({marks})",
            imported)
    skip = "" if not unreadable else (
        f" AND id NOT IN ({','.join('?' * len(unreadable))})")
    for origin, clause in _ORIGIN_BACKFILL:
        conn.execute(
            f"UPDATE memory_items SET origin = ? WHERE origin = 'unknown' "
            f"AND ({clause}){skip}", (origin, *unreadable))
    conn.execute(
        # no owner_seal here: the column may not exist yet; _migrate_owner_seal
        # seals these same rows right afterwards
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
        h = _chain_hash(prev, _chain_payload(r))
        conn.execute(
            "UPDATE memory_history SET prev_hash = ?, self_hash = ? WHERE id = ?",
            (prev, h, r["id"]),
        )
        prev = h


def _chain_payload(r: Any) -> dict[str, Any]:
    """The fields of a history row its hash is taken over."""
    return {k: r[k] for k in ("slug", "old_title", "old_body", "changed_at",
                              "changed_by", "reason")}


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
    if tip - now > 86400 and not _CLOCK_WARNED:   # once per process
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

    Single characters are dropped as BM25 noise; two-character tokens carry the
    meaning in this domain (`db`, `py`, `ci`) and are kept. Punctuation is
    discarded."""
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
    # JSON frames both strings unambiguously, and contains no literal NUL:
    # its input cannot alias the legacy title + NUL delimiter + body format.
    return hashlib.sha256(json.dumps([title, body], ensure_ascii=False).encode("utf-8")).hexdigest()


def _matches_hash(title: str, body: str, content_hash: str) -> bool:
    """Verify old records too, with the title fixed by the stored row."""
    return (content_hash == _hash(title, body) or content_hash == hashlib.sha256(
        (title + "\n\0\n" + body).encode("utf-8")).hexdigest())


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
    strength: float = 1.0
    pinned: bool = False
    lifecycle: str = "active"
    owner_seal: int = 0
    confirmed_count: int = 0
    failure_count: int = 0
    access_count: int = 0
    last_accessed_at: int | None = None
    last_decayed_at: int | None = None
    # Where the text came from (ORIGINS): owner / agent / imported (a pack) /
    # derived (a model's summary of a transcript) / unknown.
    origin: str = "unknown"
    # set only by the owner's explicit approval, never inferred from origin
    trusted_at: int | None = None
    trusted_by: str | None = None
    id: int | None = None
    content_hash: str = ""
    wordcount: int = 0
    created_at: int = 0
    updated_at: int = 0
    deleted_at: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MemoryItem":
        """The item a memory_items row holds; a column an older schema lacks
        keeps its default."""
        have = row.keys()
        kw = {f.name: row[f.name] for f in fields(cls) if f.name in have}
        kw.update({k: _parse_json_list(row[k]) for k in _LIST_COLUMNS})
        kw["pinned"] = bool(kw.get("pinned", False))
        kw["origin"] = kw.get("origin") or "unknown"
        return cls(**kw)

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


def owner_present() -> bool:
    """True when a person is at a terminal: the one owner signal every
    surface asks. The module a call comes from says nothing, since an agent
    runs the CLI through Bash as easily as a person types it."""
    try:
        if not (sys.stdin.isatty() or sys.stdout.isatty()):
            return False
    except (ValueError, AttributeError):      # closed or replaced streams
        return False
    if sys.platform != "win32":
        return True
    # Windows: isatty() is true for NUL too (`stdin=DEVNULL`); GetConsoleMode
    # succeeds on a real console only.
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        mode = wintypes.DWORD()
        for std in (-10, -11):                     # STD_INPUT_HANDLE, STD_OUTPUT_HANDLE
            handle = kernel32.GetStdHandle(std)
            if handle and handle != -1 and kernel32.GetConsoleMode(
                    wintypes.HANDLE(handle), ctypes.byref(mode)):
                return True
        return False
    except Exception:       # no ctypes, or an unexpected Windows build
        return False


class SealedRecord(Exception):
    """An agent tried to hide a record the owner wrote or approved."""


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


def _body_in_file(kind: str, body: str) -> bool:
    """Whether a body is kept in a file: the kind and length decide, never the
    row's past. A file kept on the same text left a note once a document an
    excerpt, which its own dump restored whole (INV-06); a missing file is
    repaired either way."""
    return kind == "document" or len(body) > DOC_BODY_THRESHOLD


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


def file_path(path: str | os.PathLike) -> Path:
    """``path`` resolved, and spelled as the file system stores it as far as
    it exists: one spelling per file. resolve() keeps the caller's case on
    APFS (and on Linux, where file systems that ignore case are rare), so
    `M.db` and `m.db` were two databases in one file (INV-12). Windows'
    resolve() already spells the stored name."""
    resolved = Path(path).resolve()
    if sys.platform != "darwin":
        return resolved
    import fcntl
    tail: list[str] = []
    here = resolved
    while True:
        try:
            fd = os.open(here, os.O_RDONLY | os.O_NONBLOCK)    # a FIFO does not block
        except OSError:
            if here.parent == here:
                return resolved
            tail.insert(0, here.name)
            here = here.parent
            continue
        try:
            stored = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0")
        except OSError:
            return resolved
        finally:
            os.close(fd)
        return Path(os.fsdecode(stored), *tail)


def _db_path(conn: sqlite3.Connection) -> str:
    """The database file's path (``file_path``), or ":memory:"."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(file_path(row[2])) if row and row[2] else ":memory:"
    except (sqlite3.Error, OSError, TypeError):
        return ":memory:"


def _db_inode(conn: sqlite3.Connection) -> str:
    """The database file's inode number, or "" where there is none."""
    try:
        return str(os.stat(_db_path(conn)).st_ino or "")
    except (OSError, ValueError):
        return ""


def _db_id(conn: sqlite3.Connection) -> str:
    """The random id init_schema gave this database when it was created,
    or "" for one that already held records when 0.12.0 first opened it.

    The id is stored in the file, so `cp` copies it: a file whose inode is
    not the one init_schema recorded with it is a copy (or was moved across
    file systems), another database, and gets an id derived from both."""
    try:
        meta = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('db_id', 'db_file')").fetchall())
    except sqlite3.Error:
        return ""
    db_id, ino = str(meta.get("db_id") or ""), _db_inode(conn)
    if meta.get("db_file") in (None, ino) or not ino:
        return db_id
    return hashlib.sha256(f"{db_id}\0{ino}".encode("utf-8")).hexdigest()[:32]


def _db_identity(conn: sqlite3.Connection, db_id: str | None = None) -> str:
    """8 hex chars naming this database: its resolved path and its id. The
    path alone names a place, and a newcomer there took over a moved
    database's files; the id travels with the file (INV-12)."""
    key = _db_path(conn)
    if key == ":memory:":
        key += str(id(conn))
    return _identity(key, _db_id(conn) if db_id is None else db_id)


def _identity(path: str, db_id: str) -> str:
    """The name `_db_identity` gives the database with ``db_id`` at ``path``."""
    key = path + ("\0" + db_id if db_id else "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _db_namespace(conn: sqlite3.Connection) -> str:
    """8 hex chars naming the database a body file belongs to: docs/ is shared
    by every database under one SKILLMEM_HOME. A database created before 0.12.0
    keeps the namespace its existing files use: "" for the canonical memory.db,
    its path's for any other. A file no skillmem initialised (purge's own
    connect creates one) is neither and owns no files: at a moved legacy
    database's path it took that one's (INV-12)."""
    if not _db_id(conn):
        try:
            legacy = conn.execute("SELECT EXISTS (SELECT 1 FROM meta WHERE key = 'db_id') "
                                  "OR EXISTS (SELECT 1 FROM memory_items)").fetchone()[0]
        except sqlite3.Error:
            legacy = False
        if not legacy:
            return _db_identity(conn, "\0")    # no id is "\0": no file carries it
        if _db_path(conn) == str(file_path(default_data_dir() / "memory.db")):
            return ""
    return _db_identity(conn)


def _body_filename(slug: str, *, ns: str = "", content_hash: str = "") -> str:
    """``<safe-slug>__<hash8>[-<ns8>][+<content32>].md``.

    Content-addressed (128 bits): a new body is a NEW file, never an overwrite
    of the one a committed row points at, so a rollback leaves an orphan for
    gc_body_files() and never a row whose file holds another text.
    """
    safe = _re.sub(r"[^\w.\-]+", "-", slug, flags=_re.UNICODE).strip("-") or "untitled"
    safe = safe.encode("utf-8")[:100].decode("utf-8", "ignore")   # a 300-char slug: file name too long
    h = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:8]
    if ns:
        h += f"-{ns}"
    if content_hash:
        h += f"+{content_hash[:32]}"
    return f"{safe}__{h}.md"


_BODY_FILE_RE = _re.compile(
    r"__[0-9a-f]{8}(?P<ns>-[0-9a-f]{8})?(?P<content>\+[0-9a-f]{8,64})?\.md$"
)


def _file_namespace(name: str) -> str | None:
    """The namespace suffix a body file name carries ("" or "-<ns8>"), or None
    for a pre-0.11 name: it carries none and could be any database's."""
    m = _BODY_FILE_RE.search(name)
    return (m.group("ns") or "") if m and m.group("content") else None


def _own_namespace(conn: sqlite3.Connection) -> str:
    """The suffix ``_file_namespace`` finds in this database's body files."""
    ns = _db_namespace(conn)
    return f"-{ns}" if ns else ""


def _stage_body_file(conn: sqlite3.Connection, slug: str, body: str,
                     content_hash: str) -> None:
    """Publish the body under its content-addressed name (see _body_filename).
    Bytes, as its hash was taken over them. A failed transaction leaves the file
    for gc_body_files(), never unlinks it: two writers of one text share it."""
    dest = docs_dir() / _body_filename(slug, ns=_db_namespace(conn),
                                       content_hash=content_hash)
    tmp = dest.with_suffix(dest.suffix + f".staged-{_uuid.uuid4().hex[:8]}")
    tmp.write_bytes(body.encode("utf-8"))
    os.replace(tmp, dest)


def _read_body_file(body_path: str) -> str | None:
    """An externalised body's text, byte for byte (no newline translation),
    or None when it is missing or not UTF-8: a damaged file never fails a write."""
    try:
        return (docs_dir() / body_path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def gc_body_files(conn: sqlite3.Connection) -> int:
    """Delete body files this database no longer references. Returns count.

    Only files in this database's namespace are candidates. Scanned and
    unlinked under the write lock, so an open write transaction that has
    published its files blocks this run; the 60 s grace covers staging before it.
    """
    from .export import _filename_key

    own = _own_namespace(conn)
    removed = 0
    try:
        with tx(conn):
            live = {_filename_key(r[0]) for r in conn.execute(
                "SELECT body_path FROM memory_items WHERE body_path IS NOT NULL")}
            for path in docs_dir().glob("*.md"):
                if _filename_key(path.name) in live or _file_namespace(path.name) != own:
                    continue
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


def _adopt_body_files(conn: sqlite3.Connection) -> None:
    """Give a copied database its own body files (INV-12).

    A copy's rows name the original's files, which the original's GC deletes
    once it no longer references them. Opening the copy files a verified copy
    under its own name, and fails rather than leave it exposed. No write lock
    unless a row names another namespace's file.
    """
    ns, own = _db_namespace(conn), _own_namespace(conn)
    select = "SELECT id, slug, title, body_path, content_hash FROM memory_items " \
             "WHERE body_path IS NOT NULL"

    def foreign(row: Any) -> bool:
        return _file_namespace(row["body_path"]) not in (None, own)

    if not any(foreign(r) for r in conn.execute(select)):
        return
    with tx(conn):
        for r in conn.execute(select).fetchall():   # read again under the lock
            if not foreign(r):
                continue
            text = verified_body_file(r["title"], r["body_path"], r["content_hash"])
            if text is None:
                raise OSError(f"cannot isolate body file for {r['slug']!r}: missing or invalid")
            _stage_body_file(conn, r["slug"], text, r["content_hash"])
            # a repair: the text, its hash and updated_at stay (INV-03)
            conn.execute("UPDATE memory_items SET body_path = ? WHERE id = ?", (
                _body_filename(r["slug"], ns=ns, content_hash=r["content_hash"]),
                r["id"]))


def mismatched_bodies(conn: sqlite3.Connection) -> list[str]:
    """Slugs whose externalised body no longer matches the approved text: the
    history chain says nothing about a file on disk."""
    return [r["slug"] for r in conn.execute(
        "SELECT slug, title, body_path, content_hash FROM memory_items "
        "WHERE body_path IS NOT NULL AND deleted_at IS NULL").fetchall()
        if verified_body_file(r["title"], r["body_path"], r["content_hash"]) is None]


def verified_body_file(title: str, body_path: str, content_hash: str) -> str | None:
    """The body file's text if it is the text ``content_hash`` was taken over,
    else None — missing, unreadable (not UTF-8) and altered files alike.

    Every reader that needs the full document asks here: load_body, verify,
    trust, export and the history row.
    """
    text = _read_body_file(body_path)
    if text is None or not content_hash:      # no hash: nothing to verify against
        return None
    if _matches_hash(title, text, content_hash):
        return text
    # written in text mode before 0.11.3 ("\r\n" on Windows), read back as "\n"
    legacy = text.replace("\r\n", "\n").replace("\r", "\n")
    return legacy if _matches_hash(title, legacy, content_hash) else None


def load_body(item: "MemoryItem") -> str:
    """Return the full body, materialising from disk when externalized."""
    if not item.body_path:
        return item.body
    # A file that is missing, unreadable or not the text the row's hash was
    # taken over is never served as the record: the excerpt is, and says so.
    text = verified_body_file(item.title, item.body_path, item.content_hash)
    if text is None:
        path = docs_dir() / item.body_path
        log.warning("body file missing for '%s' (%s) — returning the stored excerpt only"
                    if not path.exists() else
                    "body file for '%s' cannot be read or does not match the approved "
                    "text (%s) — serving the stored excerpt; run `skillmem verify` "
                    "and re-approve", item.slug, path)
        return item.body
    return text


EXCERPT_NOTICE = ("[skillmem: excerpt only. The whole text is unavailable (its body file "
                  "is missing or does not match this record). Do not save this back as "
                  "the record; run `skillmem verify`.]\n\n")


def is_excerpt(item: "MemoryItem", body: str) -> bool:
    """Whether ``body``, as ``load_body(item)`` returned it, is the stored
    excerpt rather than the text. Asked of the text read, never of a second
    read of the file: a repair landing in between made an excerpt pass as the
    whole text (INV-15)."""
    return bool(item.body_path) and not _matches_hash(item.title, body, item.content_hash)


def served_body(item: "MemoryItem") -> str:
    """``load_body`` for a reader: an excerpt served in place of the text
    says so, first, where a trimmed recall still shows it (INV-15). Without
    it an agent's read-edit-write made the excerpt the record's text."""
    body = load_body(item)
    return EXCERPT_NOTICE + body if is_excerpt(item, body) else body


ORIGINS = ("owner", "agent", "imported", "derived", "unknown")


def _valid_origin(origin: str | None) -> str:
    """Anything unrecognised is 'unknown' — and unknown is never trusted."""
    return origin if origin in ORIGINS else "unknown"


_KIND_RE = _re.compile(r"[a-z0-9_-][a-z0-9_ -]{0,31}")


def _repaired_kind(kind) -> str:
    """``_valid_kind`` of a kind stored before it was validated, or the
    nearest kind it accepts: each run of refused characters a '-', cut to 32
    ("how/to" is "how-to"); nothing left is "note"."""
    norm = _re.sub(r"\s+", " ", str(kind or "").strip().lower())
    if _KIND_RE.fullmatch(norm):
        return norm
    return _re.sub(r"[^a-z0-9_ -]+", "-", norm)[:32].strip() or "note"


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
    """One rule for every channel — HTTP validates too, MCP/CLI did not. An
    empty string or null is refused; neither is "private" (INV-14)."""
    v = str(visibility).strip().lower()
    if v not in VISIBILITIES:
        raise ValueError(f"invalid visibility {visibility!r}: one of {', '.join(VISIBILITIES)}")
    return v


def set_trust(conn: sqlite3.Connection, slug: str, *, trusted: bool,
              by: str = "owner", expect_hash: str | None = None,
              expect_kind: str | None = None) -> MemoryItem | None:
    """Grant or withdraw the owner's approval, the owner's act either way.

    Approval is pinned to what the owner just read (INV-01): `expect_hash` and
    `expect_kind` are compared under the lock, so a rewrite or a relabel that
    lands between the read and the approval fails instead of being approved.
    An archived record is refused: it would sit approved and out of every read.
    """
    if not owner_present():
        raise SealedRecord(f"approving '{slug}' or withdrawing its approval is the "
                           f"owner's act; it needs a person at a terminal")
    with tx(conn):     # one step: read, check the text, write
        row = conn.execute(
            "SELECT id, content_hash, kind, lifecycle FROM memory_items "
            "WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
        if not row:
            return None
        if trusted and row["lifecycle"] == "archived":
            raise MemoryConflict(
                f"'{slug}' is archived and out of search, recall and the briefing; "
                f"restore it first (`skillmem skills-archive {slug} --restore`)"
            )
        if trusted and ((expect_hash is not None and row["content_hash"] != expect_hash)
                        or (expect_kind is not None and row["kind"] != expect_kind)):
            raise MemoryConflict(
                f"'{slug}' changed since you read it; review it again "
                f"(`skillmem cat {slug}`) before approving"
            )
        if trusted:
            conn.execute("UPDATE memory_items SET trusted_at = ?, trusted_by = ?, "
                         "owner_seal = 1 WHERE id = ?", (_now(), by, row["id"]))
        else:
            conn.execute("UPDATE memory_items SET trusted_at = NULL, trusted_by = NULL "
                         "WHERE id = ?", (row["id"],))
        return get(conn, slug)     # the row as written, not after the next writer's


# Metadata a write can name. A named field is applied as given (empty or null
# clears it); an unnamed one keeps the row's value as read under the write lock
# (INV-14). On insert every field takes the item's value, which the surface
# built with its documented defaults.
_META = frozenset({"kind", "project", "visibility", "tags", "topics", "ttl_days",
                   "agent", "source_session", "attachments"})
# What only a restore carries: what the record earned, when (recency clocks
# included, which decay and the sweep read), its provenance and seal (INV-06).
_RESTORED = frozenset({"freshness_until", "strength", "origin", "owner_seal", "updated_at",
                       "created_at", "access_count", "confirmed_count", "failure_count",
                       "last_accessed_at", "last_decayed_at"})

# INVARIANTS.md §2 as data: the fields each surface can name at all, and the
# powers only some surfaces have.
#   agents:   only agents write through it: the owner is never present, even
#             when the server happens to run in the owner's terminal
#   approves: the owner writing through it at a terminal approves the text (INV-01)
#   restores: a skillmem dump is the whole record: it may reassign the author
#             (C7) and write over an archived row, whose lifecycle it carries (K1)
# Otherwise trust, the seal and a sealed row change only with the owner, which
# each mutation asks itself through _owner(surface); no caller votes on it
# (INV-02, INV-03).
SURFACES: dict[str, dict[str, Any]] = {
    "cli":     {"names": {"kind", "project", "ttl_days", "agent", "tags"}, "approves": True},
    "mcp":     {"names": {"kind", "project", "visibility", "tags", "topics", "ttl_days"},
                "agents": True},
    "http":    {"names": {"kind", "project", "visibility", "tags", "topics", "ttl_days"},
                "agents": True},
    "dump":    {"names": _META | _RESTORED, "restores": True},
    "note":    {"names": _META | {"strength"}},
    "migrate": {"names": {"kind", "source_session"}},
    "pack":    {"names": {"kind", "project", "agent", "visibility", "tags", "topics"},
                "agents": True},
    "library": {"names": _META | _RESTORED},
}



def _owner(surface: str) -> bool:
    """The owner signal as a mutation arriving through ``surface`` sees it. A
    surface only agents write through never has the owner, whatever terminal
    the server runs in; every mutation that spares the owner's rows asks here."""
    return not SURFACES[surface].get("agents") and owner_present()


_LIST_COLUMNS = ("tags", "topics", "attachments")


def _column(item: MemoryItem, key: str) -> Any:
    """``item.<key>`` as the memory_items column stores it."""
    value = getattr(item, key)
    if key in _LIST_COLUMNS:
        return _json_list(value)
    if key == "origin":
        return _valid_origin(value)
    if key == "owner_seal":
        return 1 if value else 0
    if value == "" and key in ("project", "agent", "source_session"):
        return None     # one "none", as a dump round-trips it (INV-06)
    return value


def upsert(
    conn: sqlite3.Connection,
    item: MemoryItem,
    *,
    surface: str = "library",
    explicit: Iterable[str] = (),
    reason: str | None = None,
    force: bool = False,
    check_conflicts: bool = False,
    conflict_filter: Callable[[dict[str, Any]], bool] | None = None,
    revive: bool = False,
    actor: str | None = None,
    create_only: bool = False,
    kind_only: str | None = None,
) -> MemoryItem:
    """The one write: insert ``item`` or update the row holding its slug.

    ``surface`` names the caller (a key of SURFACES) and ``explicit`` the fields
    its caller actually supplied. Everything after validation happens in one
    transaction: the row is read under the write lock, every refusal is decided
    on that read, the field policy is applied, the row and its history entry are
    written. The embedding is computed after the outermost COMMIT (see tx).

    Refused: a slug that exists when ``create_only``; a tombstone unless
    ``revive``; a live row of another kind than ``kind_only``; an archived row
    (restore it first), except by a restore; different text without ``reason``
    or ``force``; any change to a sealed row without the owner at a terminal;
    a new author on an existing row, except by a restore or the owner.

    ``item`` becomes the stored row: title and body scrubbed, a long body
    replaced by its excerpt (``load_body`` reads the whole text back).
    """
    policy = SURFACES[surface]
    named = set(explicit)
    if named - policy["names"]:
        raise ValueError(f"{surface} cannot set {', '.join(sorted(named - policy['names']))}")
    if not (item.slug or "").strip():
        raise ValueError("slug is required")   # `write --slug ""` stored a record nothing could address
    item.kind = _valid_kind(item.kind)
    item.visibility = _valid_visibility(item.visibility)
    # INV-14: each field is exactly its type (a bool is not a TTL, a mapping not tags)
    if item.ttl_days is not None and (type(item.ttl_days) is not int
                                      or not 1 <= item.ttl_days <= 3650):
        raise ValueError(f"invalid ttl_days {item.ttl_days!r}: 1..3650")
    for key in ("tags", "topics", "attachments"):
        value = getattr(item, key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"invalid {key} {value!r}: a list of strings")
    for key in ("project", "agent", "source_session"):
        if not isinstance(getattr(item, key), (str, type(None))):
            raise ValueError(f"invalid {key} {getattr(item, key)!r}: a string")
    if isinstance(item.strength, bool) or not 0 <= float(item.strength) <= STRENGTH_CAP:
        # above the cap, the next reinforcement "capped" a rule down (INV-03)
        raise ValueError(f"invalid strength {item.strength!r}: 0..{STRENGTH_CAP}")
    item.title = scrub(item.title)
    full_body = scrub(item.body)
    content_hash = _hash(item.title, full_body)

    if check_conflicts and not force and conn.execute(
            "SELECT 1 FROM memory_items WHERE slug = ?", (item.slug,)).fetchone() is None:
        # advisory, so outside the lock: the FTS scan must not hold other writers
        conflicts = find_conflicts(conn, item.title, _make_excerpt(full_body),
                                   visible=conflict_filter)
        if conflicts:
            raise MemoryConflict(
                "duplicate-candidates:" + json.dumps(conflicts, ensure_ascii=False))

    owner = _owner(surface)
    with tx(conn):
        row = conn.execute("SELECT * FROM memory_items WHERE slug = ?",
                           (item.slug,)).fetchone()
        same_text = (row is not None and row["title"] == item.title
                     and _matches_hash(item.title, full_body, row["content_hash"]))
        if same_text:
            # Keep legacy approvals and body names on a true no-op or repair.
            # A text change always uses the unambiguous hash above.
            content_hash = row["content_hash"]
        if row is not None:
            _refuse(item.slug, row, policy, create_only=create_only, revive=revive,
                    kind_only=kind_only, same_text=same_text, reason=reason, force=force)
        now = _now()
        new = _resolve(conn, item, row, named, policy, owner=owner, now=now,
                       full_body=full_body, content_hash=content_hash,
                       same_text=same_text, revive=revive)
        # the links are the stored text's words, not a caller's list: taken
        # from the raw body they kept what scrub removed, past approval (INV-07)
        _replace_links_inner(conn, item.slug, extract_wikilinks(full_body))
        if row is None:
            conn.execute(
                f"INSERT INTO memory_items ({', '.join(new)}) "
                f"VALUES ({', '.join('?' * len(new))})", tuple(new.values()))
        else:
            changed = {k: v for k, v in new.items() if v != row[k]}
            # a body file repair or a rebuilt lexical index: same text, same record
            repair = same_text and changed.keys() <= _REPAIR
            if changed and row["owner_seal"] and not owner and not repair:   # INV-03
                raise SealedRecord(
                    f"'{item.slug}' is the owner's record (written or approved by "
                    f"them); only the owner changes it. Write a proposal under a "
                    f"new slug instead.")
            if "agent" in changed and not (owner or policy.get("restores")):
                raise MemoryConflict(
                    f"'{item.slug}' was written by {row['agent']!r}; only the owner "
                    f"at a terminal reassigns authorship")
            if not changed:
                item.__dict__.update(MemoryItem.from_row(row).__dict__)
                return item
            if not same_text:
                _append_history(conn, row, now, actor or item.agent,
                                reason or "force overwrite")
                changed["embedding"] = None   # it described the old text
            elif "deleted_at" in changed:     # a revive is a transition (INV-13)
                _append_history(conn, row, now, actor or item.agent,
                                "restored from deleted")
            # a restore names the age it brings back, even when it equals the row's;
            # a repair is not an edit, and moving the age made a stale rule fresh
            if not repair:
                changed.setdefault("updated_at", new.get("updated_at", now))
            sets = ", ".join(f"{k} = ?" for k in changed)
            conn.execute(f"UPDATE memory_items SET {sets} WHERE id = ?",
                         (*changed.values(), row["id"]))
        stored = conn.execute("SELECT * FROM memory_items WHERE slug = ?",
                              (item.slug,)).fetchone()
        if not same_text:
            _set_embedding(conn, stored["id"], item.title, full_body, content_hash)
    item.__dict__.update(MemoryItem.from_row(stored).__dict__)
    return item


def upsert_skill(conn: sqlite3.Connection, item: MemoryItem, **kw: Any) -> MemoryItem:
    """``upsert`` for a learned skill: every learn surface (CLI, MCP, HTTP)
    comes through here, and a slug holding another kind is refused. The verb
    names the kind (INV-01)."""
    kw["explicit"] = {*kw.get("explicit", ()), "kind"}
    return upsert(conn, item, kind_only="skill", **kw)


# Columns that hold the text or index it, not describe it: a same-text write
# changing only these repairs the row (INV-03).
_REPAIR = frozenset({"body", "body_path", "stemmed"})


def _refuse(slug: str, row: Any, policy: dict[str, Any], *, create_only: bool,
            revive: bool, kind_only: str | None, same_text: bool,
            reason: str | None, force: bool) -> None:
    """Every refusal of a write to an existing row, decided on the row read
    under the write lock."""
    if create_only:     # the caller's permission check saw the slug free
        raise MemoryConflict(f"slug '{slug}' was created concurrently by another writer")
    if row["deleted_at"] is not None:
        if not revive:      # a tombstone is still a record (INV-08)
            raise MemoryConflict(
                f"slug '{slug}' belongs to a deleted record; restore it "
                f"(revive) or pick another slug")
        return
    if kind_only and row["kind"] != kind_only:
        raise MemoryConflict(f"slug '{slug}' already holds a {row['kind']}; "
                             f"pick another slug or update it instead")
    if row["lifecycle"] == "archived" and not policy.get("restores"):
        # found by no read (INV-08), and un-archiving is the owner's act
        raise MemoryConflict(
            f"'{slug}' is archived; restore it first (skillmem skills-restore {slug})")
    if not same_text and not (reason or force):
        raise MemoryConflict(
            f"slug '{slug}' already exists with different text; "
            f"overwrite it through an explicit update, or pick another slug")


def _resolve(conn: sqlite3.Connection, item: MemoryItem, row: Any, named: set[str],
             policy: dict[str, Any], *, owner: bool, now: int, full_body: str,
             content_hash: str, same_text: bool, revive: bool) -> dict[str, Any]:
    """The columns this write sets. On insert, every one; on an update, what
    the caller named, what the text carries, and what ownership decides."""
    # the origin this write states: the item's, unless a surface that can
    # name it left it out; then the row keeps its own, and it mints no seal (INV-14)
    origin = (_valid_origin(item.origin) if row is None or "origin" in named
              or "origin" not in policy["names"] else None)
    if row is None:
        new = {k: _column(item, k) for k in (
            _META | {"strength", "origin"})}
        # a birth date and a deadline only from a surface that restores them
        # (INV-14); a named value is the value, null and 0 too (INV-06)
        restores = policy["names"] >= {"created_at", "freshness_until"}
        new.update(freshness_until=item.freshness_until if "freshness_until" in named else (
                       item.freshness_until if restores else None) or (
                       now + item.ttl_days * 86400 if item.ttl_days else None),
                   created_at=item.created_at if "created_at" in named else (
                       item.created_at if restores else None) or now,
                   updated_at=item.updated_at if "updated_at" in named else now)
        for k in ("access_count", "confirmed_count", "failure_count",
                  "last_accessed_at", "last_decayed_at"):
            if k in named:
                new[k] = getattr(item, k)
    else:
        new = {k: _column(item, k) for k in named
               if k not in ("freshness_until", "owner_seal", "updated_at")}
        if not same_text and origin:
            new["origin"] = origin   # origin describes the text
        # the deadline goes with the TTL; one the caller names is the deadline,
        # null too (it clears it), or it was acknowledged and dropped
        if "freshness_until" in named:
            new["freshness_until"] = item.freshness_until
        elif "ttl_days" in named and (item.ttl_days != row["ttl_days"] or not same_text):
            new["freshness_until"] = now + item.ttl_days * 86400 if item.ttl_days else None
        if "updated_at" in named:
            new["updated_at"] = item.updated_at
        if revive and row["deleted_at"] is not None:    # back live and visible (INV-08)
            new.update(deleted_at=None, lifecycle="active")

    def eff(k: str) -> Any:
        return new[k] if k in new else row[k]

    body_path = (_body_filename(item.slug, ns=_db_namespace(conn), content_hash=content_hash)
                 if _body_in_file(eff("kind"), full_body) else None)
    if body_path and (row is None or row["body_path"] != body_path
                      or _read_body_file(body_path) != full_body):
        _stage_body_file(conn, item.slug, full_body, content_hash)
    new["body_path"] = body_path
    if not same_text or body_path != row["body_path"]:
        new["body"] = _make_excerpt(full_body) if body_path else full_body
    if not same_text:
        new.update(title=item.title, content_hash=content_hash,
                   wordcount=_wordcount(full_body))
    if not same_text or {"tags", "topics"} & named:
        # tags and topics are in the lexical index, as the row will have them
        new["stemmed"] = _stem_text(f"{item.title}\n{full_body}\n" + " ".join(
            _parse_json_list(eff("tags")) + _parse_json_list(eff("topics"))))

    # Approval belongs to the text and kind approved (INV-01): the owner writing
    # at a terminal through a surface that approves is the approval; any other
    # change of either clears it. A kind the write does not name is approved
    # only if it already was: the owner never saw an agent's label.
    if policy.get("approves") and owner and (
            row is None or "kind" in named or row["trusted_at"] is not None):
        if row is None or not same_text or row["trusted_at"] is None:
            new.update(trusted_at=now, trusted_by="cli-tty")
    elif row is None or not same_text or eff("kind") != row["kind"]:
        new.update(trusted_at=None, trusted_by=None)
    # The seal is minted only with the owner at a terminal (INV-02) and never
    # cleared; a caller that names it (a dump) gets that one, not the origin's (INV-06).
    if owner and (item.owner_seal if "owner_seal" in named
                  else origin == "owner"):
        new["owner_seal"] = 1
    elif row is None:
        new["owner_seal"] = 0
    if row is None:
        new["slug"] = item.slug
    return new


def _append_history(conn: sqlite3.Connection, row: Any, now: int,
                    changed_by: str | None, reason: str) -> None:
    """The one history row: the version a text change, delete or lifecycle
    move replaces, as read under the write lock (INV-13). ``row`` carries
    slug, title, body, body_path and content_hash. An externalised body is its
    verified file, else the excerpt, saying so as `served_body` does (INV-15)."""
    old_body = row["body"]
    if row["body_path"]:
        old_body = (text if (text := verified_body_file(row["title"], row["body_path"], row["content_hash"]))
                    is not None else EXCERPT_NOTICE + old_body)
    prev_hash = _last_chain_hash(conn)
    entry = {"slug": row["slug"], "old_title": row["title"], "old_body": old_body,
             "changed_at": _chain_clock(conn, now), "changed_by": changed_by, "reason": reason}
    conn.execute(
        "INSERT INTO memory_history (slug, old_title, old_body, changed_at, changed_by,"
        " reason, prev_hash, self_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (*entry.values(), prev_hash, _chain_hash(prev_hash, entry)))


def _set_embedding(conn: sqlite3.Connection, item_id: int | None, title: str, body: str,
                   content_hash: str) -> bool:
    """Best-effort embedding write, never under the write lock (INV-09): inside
    a transaction it waits for tx() to commit. A row without one falls back to
    BM25; ``reindex-embeddings`` backfills it."""
    if item_id is None:
        return False
    if conn.in_transaction:
        _deferred_embeddings.setdefault(id(conn), []).append(
            (item_id, title, body, content_hash))
        return False
    from . import embed as _embed

    if not _embed.semantic_enabled():
        return False
    blob = _embed.embed_text(_embed.doc_text(title, body))
    if blob is None:
        return False
    try:
        return conn.execute(
            # CAS on the text: a newer write may have landed while the model ran
            "UPDATE memory_items SET embedding = ? WHERE id = ? AND content_hash = ?",
            (blob, item_id, content_hash),
        ).rowcount > 0
    except sqlite3.Error as exc:
        log.warning("could not store embedding for id=%s: %s", item_id, exc)
        return False


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
        updated += _set_embedding(conn, row["id"], row["title"], body, row["content_hash"])
    return {"updated": updated, "total": len(rows)}


def soft_delete(conn: sqlite3.Connection, slug: str, reason: str, *,
                surface: str = "library") -> bool:
    """Tombstone a live record and write its history row; False if there is
    none. Deleting a sealed record is the owner's call (INV-03), which the
    mutation asks `_owner(surface)` itself: no caller vouches for the owner."""
    with tx(conn):
        row = conn.execute(
            "SELECT id, slug, title, body, body_path, content_hash, owner_seal "
            "FROM memory_items WHERE slug = ? AND deleted_at IS NULL", (slug,)
        ).fetchone()
        if row is None:
            return False
        if row["owner_seal"] and not _owner(surface):
            raise SealedRecord(
                f"'{slug}' is the owner's record (written or approved by them); "
                f"deleting it is the owner's call: skillmem rm {slug}"
            )
        now = _now()
        _append_history(conn, row, now, None, f"deleted: {reason}")
        conn.execute("UPDATE memory_items SET deleted_at = ? WHERE id = ?", (now, row["id"]))
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
        expected_self = _chain_hash(prev_actual, _chain_payload(r))
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


# --------------------------------------------------------------------------- #
# read / search
# --------------------------------------------------------------------------- #


def get(conn: sqlite3.Connection, slug: str) -> MemoryItem | None:
    row = conn.execute(
        "SELECT * FROM memory_items WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    ).fetchone()
    return MemoryItem.from_row(row) if row else None


def read_record(conn: sqlite3.Connection, slug: str, *,
                with_history: bool = False) -> dict[str, Any] | None:
    """The by-slug read behind `cat`, mem_get and /get: the row, its whole
    body, its links and its history from one committed state (INV-04), so a
    reader never pairs old text with the history of an edit it did not see.
    ``links_in`` holds the live source rows, for a caller that filters them."""
    with snapshot(conn):
        item = get(conn, slug)
        if item is None:
            return None
        body = served_body(item)
        # the words of the text served, which the owner approved (INV-07)
        return {"item": item, "body": body,
                "links_out": sorted(extract_wikilinks(body)),
                "links_in": [row for src in links_to(conn, slug)
                             if (row := get(conn, src)) is not None],
                "history": history(conn, slug) if with_history else []}


def history(conn: sqlite3.Connection, slug: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM memory_history WHERE slug = ? ORDER BY changed_at DESC, id DESC",
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


# the narrow columns _visibility_view reads, for a walk that reads no bodies
_VIEW_COLUMNS = "id, slug, strength, visibility, topics, agent, trusted_at"


def _visibility_view(row: Any) -> dict[str, Any]:
    """What a ``visible`` predicate is shown of a row, the same on every read."""
    return {"visibility": row["visibility"], "agent": row["agent"],
            "trusted_at": row["trusted_at"], "slug": row["slug"],
            "strength": row["strength"], "topics": _parse_json_list(row["topics"])}


def _rank_filter(kind: str | None = None, project: str | None = None,
                 exclude_kinds: tuple[str, ...] = (), prefix: str = "") -> tuple[str, list[Any]]:
    """The SQL filter of a ranked read: the ranking and the refetch of its
    winners (_fetch_live) state it once, so a row that stopped matching in
    between is not returned (INV-04)."""
    where = [f"{prefix}deleted_at IS NULL", f"{prefix}lifecycle != 'archived'"]
    params: list[Any] = []
    if kind:
        where.append(f"{prefix}kind = ?")
        params.append(kind)
    if project:
        where.append(f"{prefix}project = ?")
        params.append(project)
    if exclude_kinds:
        where.append(f"{prefix}kind NOT IN ({','.join('?' * len(exclude_kinds))})")
        params.extend(exclude_kinds)
    return " AND ".join(where), params


def _fetch_live(conn: sqlite3.Connection, ids: list[int],
                visible: Callable[[dict[str, Any]], bool] | None = None,
                **filters: Any) -> dict[int, Any]:
    """Rows for ids a ranking picked, re-read with the ranking's own filter
    (_rank_filter) and the caller's ``visible`` asked again of the row whose
    text is returned (INV-04). Every ranked read fetches through here."""
    if not ids:
        return {}
    clause, params = _rank_filter(**filters)
    return {r["id"]: r for r in conn.execute(
        f"SELECT * FROM memory_items WHERE id IN ({','.join('?' * len(ids))}) "
        f"AND {clause}", [*ids, *params]).fetchall()
        if visible is None or visible(_visibility_view(r))}


def _kind_filter(kind: str | None) -> str | None:
    """A kind filter as writes normalise kinds ("Reference" finds "reference");
    raises ValueError for one nothing can match."""
    return _valid_kind(kind) if kind else kind


def list_items(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    project: str | None = None,
    limit: int = 50,
    recent: bool = True,
    visible: Callable[[dict[str, Any]], bool] | None = None,
) -> list[MemoryItem]:
    try:
        kind = _kind_filter(kind)
    except ValueError:
        return []
    where, params = _rank_filter(kind, project)
    order = "updated_at DESC" if recent else "slug ASC"
    if visible is None:
        sql = f"SELECT * FROM memory_items WHERE {where} ORDER BY {order} LIMIT ?"
        rows = conn.execute(sql, [*params, limit]).fetchall()
        return [MemoryItem.from_row(r) for r in rows]
    # a filtered listing walks the order on narrow columns and stops at the
    # first `limit` rows the caller may see; full rows are read only for those
    cur = conn.execute(
        f"SELECT {_VIEW_COLUMNS} FROM memory_items WHERE {where} ORDER BY {order}", params)
    keep: list[int] = []
    for r in cur:
        if visible(_visibility_view(r)):
            keep.append(r["id"])
            if len(keep) >= limit:
                cur.close()
                break
    if not keep:
        return []
    fetched = _fetch_live(conn, keep, visible, kind=kind, project=project)
    return [MemoryItem.from_row(fetched[i]) for i in keep if i in fetched]


def _escape_fts(query: str) -> str:
    """Escape a user query for FTS5 MATCH against ``mem_fts_stem``.

    Tokens are Snowball-stemmed then quoted as prefix matches. We use OR
    semantics across tokens (standard search-engine behavior) and rely on
    BM25 to rank documents that match more of them higher. Implicit AND
    would be too strict for natural-language queries — a 5-word question
    almost never has all 5 stems in a single short message.
    """
    # tokenised as the documents are, not on whitespace: tool-recall's query
    # is a file path
    return _escape_fts_or(_WORD_RE.findall(query), unique=True)


def _escape_fts_or(tokens: Iterable[str], *, unique: bool = False) -> str:
    """Stem-aware OR query of prefix matches; ``'""'`` when nothing is left."""
    parts: list[str] = []
    for raw in tokens:
        stem = _stem_word(raw)
        part = '"' + stem.replace('"', '""') + '"*'
        if len(stem) >= 2 and not (unique and part in parts):
            parts.append(part)
    return " OR ".join(parts) if parts else '""'


def _snippet_for(body: str, query: str, *, around: int = 90) -> str:
    """Render an excerpt around the first matched stem (cheap, language-aware)."""
    notice = EXCERPT_NOTICE if body.startswith(EXCERPT_NOTICE) else ""
    body = body[len(notice):]
    if not body:
        return notice
    stems = [_stem_word(t) for t in query.split() if len(_stem_word(t)) >= 2]
    if not stems:
        return notice + body[: around * 2] + ("…" if len(body) > around * 2 else "")
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
        return notice + body[: around * 2] + ("…" if len(body) > around * 2 else "")
    start = max(0, idx - around)
    end = min(len(text), idx + around)
    pre = "…" if start > 0 else ""
    post = "…" if end < len(text) else ""
    return notice + pre + text[start:end].replace("\n", " ") + post


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
    # Excluding kinds AFTER the candidate pool would drop the answer: the pool is
    # capped, so a wall of session recaps can fill it and hide every skill.
    clause, filters = _rank_filter(kind, project, exclude_kinds, "m.")
    sql = f"""
        SELECT m.id AS id, bm25(mem_fts_stem) AS r
        FROM mem_fts_stem
        JOIN memory_items m ON m.id = mem_fts_stem.rowid
        WHERE mem_fts_stem MATCH ? AND {clause}
        ORDER BY r
        LIMIT ?
    """
    params: list[Any] = [_escape_fts(query), *filters, -1 if pool is None else pool]
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
    makes the caller degrade to pure BM25 — and inside a caller's transaction:
    the query is embedded here, and the model never runs under the write lock
    (INV-09; a search in a caller's tx locked out every other writer).
    """
    from . import embed as _embed

    if conn.in_transaction:
        return []
    qb = _embed.pack_query(query)
    if qb is None:
        return []
    try:
        import numpy as np
    except Exception:
        return []
    clause, params = _rank_filter(kind, project, exclude_kinds)
    rows = conn.execute("SELECT id, embedding FROM memory_items "
                        f"WHERE embedding IS NOT NULL AND {clause}", params).fetchall()
    if not rows:
        return []
    q = np.frombuffer(qb, dtype="float32")
    # one blob of another width (a truncated write, a model swap) made np.stack
    # raise, and every search failed while semantic recall was on
    rows = [r for r in rows if len(r["embedding"]) == len(qb)]
    if not rows:
        return []
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
    """The first ``limit`` of ``ids`` (rank order kept) that ``visible`` accepts,
    asked within the ranking, before the limit: rows filtered after it could
    fill the limit and keep an approved one out. One narrow query per 500 ids,
    no body reads."""
    if visible is None:
        return ids[:limit]
    out: list[int] = []
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = conn.execute(
            f"SELECT {_VIEW_COLUMNS} FROM memory_items "
            f"WHERE id IN ({','.join('?' * len(chunk))})", chunk,
        ).fetchall()
        meta = {r["id"]: _visibility_view(r) for r in rows}
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
    try:
        kind = _kind_filter(kind)
    except ValueError:
        return []
    ids = hybrid_rank_ids(conn, query, kind=kind, project=project, limit=limit,
                          exclude_kinds=exclude_kinds, visible=visible)
    if not ids:
        return []
    # callers classify trust by what this read returns
    by_id = _fetch_live(conn, ids, visible, kind=kind, project=project,
                        exclude_kinds=exclude_kinds)
    rows = [by_id[i] for i in ids if i in by_id]  # preserve fused order

    now = _now()
    out: list[dict[str, Any]] = []
    for pos, row in enumerate(rows, start=1):
        d = dict(row)
        # Raw float32 blob: garbage in CLI --format json and a serialization
        # 500 in the HTTP layer. Nothing downstream reads it from a hit.
        d.pop("embedding", None)
        # the index copy of title and body: emitted beside the framed body it
        # was the unapproved text again, outside the frame (INV-07)
        d.pop("stemmed", None)
        d["tags"] = _parse_json_list(d.get("tags"))
        d["topics"] = _parse_json_list(d.get("topics"))
        d["attachments"] = _parse_json_list(d.get("attachments"))
        label, stale = _freshness(now, d["updated_at"], d.get("freshness_until"))
        d["freshness"] = label
        d["stale_days"] = stale
        if row["body_path"]:
            # the text, or the excerpt saying it is one, as recall and get serve (INV-15)
            d["body"] = served_body(MemoryItem.from_row(row))
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

    FTS5 BM25 surfaces candidates, then the inclusion overlap
    ``|A ∩ B| / min(|A|, |B|)`` of their word bags decides: 70% of one doc's
    words in the other is a duplicate. ``visible`` drops candidates the caller
    may not see, before the top ``candidates`` are taken: a 409 naming another
    agent's private record is a read through the trust boundary.
    """
    bag = _word_bag(title + "\n" + body)
    if len(bag) < 5:
        return []  # too short to make a meaningful overlap claim

    fts_query = _escape_fts_or(list(bag)[:32])
    if fts_query == '""':
        return []
    try:
        # With a filter the walk has no LIMIT and stops at `candidates` visible
        # rows; narrow columns, as an unlimited walk sorts every match first.
        rows = conn.execute(
            "SELECT m.id, m.slug, m.strength, m.visibility, m.topics, m.agent, m.trusted_at "
            "FROM mem_fts_stem "
            "JOIN memory_items m ON m.id = mem_fts_stem.rowid "
            "WHERE mem_fts_stem MATCH ? AND m.deleted_at IS NULL "
            "ORDER BY bm25(mem_fts_stem) LIMIT ?",
            (fts_query, candidates if visible is None else -1),
        )
    except sqlite3.OperationalError as exc:
        log.warning("find_conflicts FTS query failed (%s); treating as empty", exc)
        return []

    conflicts: list[dict[str, Any]] = []
    scored = 0
    for row in rows:
        if exclude_slug and row["slug"] == exclude_slug:
            continue
        if visible is not None and not visible(_visibility_view(row)):
            continue
        if scored >= candidates:          # the top-N *visible* by BM25, as before the filter
            rows.close()
            break
        scored += 1
        text = conn.execute(
            "SELECT * FROM memory_items WHERE id = ? AND deleted_at IS NULL", (row["id"],)
        ).fetchone()
        if text is None or (visible is not None and not visible(_visibility_view(text))):
            continue                      # deleted or hidden between the walk and now
        other = _word_bag(text["title"] + "\n" + text["body"])
        if not other:
            continue
        overlap = len(bag & other) / min(len(bag), len(other))
        if overlap >= threshold:
            # no title: an error message is never framed (INV-07)
            conflicts.append({"slug": row["slug"], "overlap": round(overlap, 3)})
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
    # as _valid_kind normalises: `inject --types Feedback` found nothing
    kinds = [_re.sub(r"\s+", " ", k.strip().lower()) for k in kinds or ["user", "feedback"]]
    char_budget = budget_tokens * _CHARS_PER_TOKEN
    sections: list[dict[str, Any]] = []
    used = 0
    omitted = 0
    unapproved = 0

    ordered = [k for k in _INJECT_KIND_ORDER if k in kinds] + [
        k for k in kinds if k not in _INJECT_KIND_ORDER
    ]

    for kind in ordered:
        # approved titles only: the briefing has no room for a frame (INV-07)
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
            "AND lifecycle != 'archived' AND trusted_at IS NULL", (kind,),
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

    # the owner's records an agent rewrote since, which lost their approval
    # and so left the briefing: named, or a rule just stops arriving
    revoked = [r["slug"] for r in conn.execute(
        "SELECT slug FROM memory_items WHERE owner_seal = 1 AND trusted_at IS NULL "
        "AND deleted_at IS NULL AND lifecycle != 'archived' "
        f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY updated_at DESC LIMIT 20",
        tuple(kinds),
    ).fetchall()] if kinds else []

    return {
        "sections": sections,
        "approx_tokens": used // _CHARS_PER_TOKEN,
        "unapproved": unapproved,
        "awaiting_reapproval": revoked,
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
    visible: Callable[[dict[str, Any]], bool] | None = None,
    surface: str = "library",
) -> dict[str, Any] | None:
    """Record that a skill was used, and move its strength by the evidence.

    ``evidence`` is one of ``EVIDENCE_WEIGHTS``. ``self_report`` (the default,
    and what plain retrieval produces) refreshes recency and the access count
    but leaves strength alone. ``test_passed`` / ``diff_accepted`` /
    ``user_confirmed`` are outside signals and raise it. ``failure`` says the
    task went wrong after the skill was applied and lowers it.

    Only a live, visible skill is reinforced, and ``visible`` (the HTTP
    caller's predicate) is asked of the row read under the write lock.
    """
    if evidence not in EVIDENCE_WEIGHTS:
        raise ValueError(
            f"unknown evidence {evidence!r}; expected one of "
            f"{', '.join(sorted(EVIDENCE_WEIGHTS))}"
        )
    now = _now()
    with tx(conn):
        row = conn.execute(
            f"SELECT {_VIEW_COLUMNS} FROM memory_items WHERE slug = ? AND deleted_at IS NULL "
            "AND kind = 'skill' AND lifecycle != 'archived'",
            (slug,),
        ).fetchone()
        if not row or (visible is not None and not visible(_visibility_view(row))):
            return None
        # relative arithmetic, so concurrent confirmations add up
        if evidence == "failure":
            # an agent's report does not walk the owner's rule below
            # tool-recall's floor (INV-03); the floor never lifts a strength
            conn.execute(
                "UPDATE memory_items SET strength = CASE WHEN owner_seal = 1 AND ? "
                "THEN strength ELSE MIN(strength, MAX(?, strength * ?)) END, "
                "access_count = access_count + 1, last_accessed_at = ?, "
                "failure_count = failure_count + 1 WHERE id = ?",
                (not _owner(surface), DECAY_FLOOR, FAILURE_FACTOR, now, row["id"]),
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
    return {"slug": slug, "strength": round(fresh["strength"], 3),
            "access_count": fresh["access_count"], "evidence": evidence,
            "confirmed_count": fresh["confirmed_count"],
            "failure_count": fresh["failure_count"]}


def reinforce_retrieved(
    conn: sqlite3.Connection, slugs: Iterable[str], *,
    visible: Callable[[dict[str, Any]], bool] | None = None, surface: str = "library",
) -> dict[str, dict[str, Any]]:
    """``reinforce`` each retrieved slug, as a read's bookkeeping: it never
    fails the read or holds it up. Each waits 150 ms for the write lock at
    most, and the first that cannot get it ends the pass. Returns the results
    by slug."""
    done: dict[str, dict[str, Any]] = {}
    for slug in slugs:
        prev = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.execute("PRAGMA busy_timeout = 150")
        try:
            r = reinforce(conn, slug, visible=visible, surface=surface)
        except sqlite3.OperationalError:
            break
        finally:
            conn.execute(f"PRAGMA busy_timeout = {int(prev)}")
        if r:
            done[slug] = r
    return done


def set_pinned(
    conn: sqlite3.Connection, slug: str, pinned: bool, *, surface: str = "library"
) -> dict[str, Any] | None:
    """Pin or unpin a record. A pinned record never decays and is never
    archived: for a rule that matters because it is rarely needed. The flag
    only: neither updated_at (pinning is not an edit) nor the lifecycle (an
    archived row comes back through set_archived alone)."""
    with tx(conn):
        row = conn.execute(
            "SELECT id, pinned, lifecycle, owner_seal FROM memory_items "
            "WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
        if not row:
            return None
        if row["owner_seal"] and bool(row["pinned"]) != pinned and not _owner(surface):
            # an agent unpinning the owner's rule hands it to decay (INV-03)
            raise SealedRecord(f"'{slug}' is the owner's record; only the owner "
                               f"pins or unpins it: skillmem pin {slug}")
        conn.execute("UPDATE memory_items SET pinned = ? WHERE id = ?",
                     (1 if pinned else 0, row["id"]))
    return {"slug": slug, "pinned": pinned, "changed": bool(row["pinned"]) != pinned,
            "lifecycle": row["lifecycle"]}


def decay_stale(
    conn: sqlite3.Connection,
    *,
    days_threshold: int = 14,
    kind: str = "skill",
) -> list[dict[str, Any]]:
    """Ebbinghaus decay: reduce strength of skills not accessed recently.

    One step per elapsed threshold, idleness measured from the last access or
    else the birth, and never a pinned or sealed record (INV-03). Read and
    written under one lock: a pin or approval in between is honoured."""
    now = _now()
    days_threshold = max(1, int(days_threshold))   # 0 or negative compounded on every run
    cutoff = now - days_threshold * 86400
    decayed: list[dict[str, Any]] = []
    with tx(conn):
        for r in conn.execute(
            "SELECT id, slug, strength FROM memory_items "
            "WHERE kind = ? AND deleted_at IS NULL AND strength > ? "
            "AND pinned = 0 AND owner_seal = 0 "
            "AND COALESCE(last_accessed_at, created_at) <= ? "
            "AND COALESCE(last_decayed_at, 0) <= ?",
            (kind, DECAY_FLOOR, cutoff, cutoff),
        ).fetchall():
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
    Pinned and sealed records sit out both (INV-03). Each transition writes
    its history row (INV-13), under one lock with the chain head it extends.
    """
    now = _now()
    with tx(conn):
        stale_cut = now - STALE_AFTER_DAYS * 86400
        archive_cut = now - ARCHIVE_AFTER_DAYS * 86400

        # COALESCE: a never-recalled skill counts idle time from its creation.
        archive_rows = conn.execute(
            "SELECT id, slug, title, body, body_path, content_hash, strength, last_accessed_at "
            "FROM memory_items "
            "WHERE kind = ? AND deleted_at IS NULL AND lifecycle != 'archived' "
            "AND pinned = 0 AND owner_seal = 0 "
            "AND strength <= ? AND COALESCE(last_accessed_at, created_at) < ?",
            (kind, DECAY_FLOOR, archive_cut),
        ).fetchall()
        _backup_skills(archive_rows, "archive")
        archived = [r["slug"] for r in archive_rows]
        for r in archive_rows:
            _append_history(conn, r, now, "sweep", "archived by nightly sweep")
            conn.execute(
                "UPDATE memory_items SET lifecycle = 'archived' WHERE id = ?", (r["id"],)
            )

        stale_rows = conn.execute(
            "SELECT id, slug, title, body, body_path, content_hash FROM memory_items "
            "WHERE kind = ? AND deleted_at IS NULL AND lifecycle = 'active' "
            "AND pinned = 0 AND owner_seal = 0 "
            "AND COALESCE(last_accessed_at, created_at) < ?",
            (kind, stale_cut),
        ).fetchall()
        staled = [r["slug"] for r in stale_rows]
        for r in stale_rows:
            _append_history(conn, r, now, "sweep", "stale by nightly sweep")
            conn.execute(
                "UPDATE memory_items SET lifecycle = 'stale' WHERE id = ?", (r["id"],)
            )
    return {"staled": staled, "archived": archived}


def lifecycle_counts(
    conn: sqlite3.Connection, *, kind: str | None = None
) -> dict[str, int]:
    """Count records per lifecycle state; kind=None counts every kind."""
    rows = conn.execute(
        "SELECT lifecycle, COUNT(*) c FROM memory_items "
        "WHERE (? IS NULL OR kind = ?) AND deleted_at IS NULL GROUP BY lifecycle",
        (kind, kind),
    ).fetchall()
    return {r["lifecycle"]: r["c"] for r in rows}


def set_archived(
    conn: sqlite3.Connection, slug: str, archived: bool = True, *,
    by: str | None = None, surface: str = "library"
) -> dict[str, Any] | None:
    """Archive a record (out of search, recall and inject; kept, reversible)
    or bring it back. A pinned record is refused — pin means "never archive".

    Moving a sealed record either way is the owner's call (INV-03), asked of
    `_owner(surface)` under the write lock. Only a real transition writes a
    history row (INV-13) or, on a restore, refreshes recency and floors
    strength so the next sweep does not archive it again. The lifecycle only:
    updated_at is the text's age.
    """
    with tx(conn):
        row = conn.execute(
            "SELECT id, slug, pinned, lifecycle, title, body, body_path, content_hash, "
            "owner_seal FROM memory_items "
            "WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
        if not row:
            return None
        moving = ((row["lifecycle"] != "archived") if archived
                  else (row["lifecycle"] != "active"))
        if moving and not _owner(surface) and row["owner_seal"]:
            raise SealedRecord(
                f"'{slug}' is the owner's record (written or approved by them); "
                f"an agent cannot {'archive' if archived else 'restore'} it. "
                f"The owner can: skillmem skills-archive {slug}"
                + ("" if archived else " --restore")
            )
        if archived and moving and row["pinned"]:   # archiving an archived row is a no-op
            raise ValueError(f"'{slug}' is pinned; unpin it before archiving it")
        if moving:
            _append_history(
                conn, row, _now(), by,
                "archived" if archived else f"restored from {row['lifecycle']}",
            )
            if archived:
                conn.execute(
                    "UPDATE memory_items SET lifecycle = 'archived' WHERE id = ?", (row["id"],)
                )
            else:
                conn.execute(
                    "UPDATE memory_items SET lifecycle = 'active', "
                    "strength = MAX(strength, ?), last_accessed_at = ? WHERE id = ?",
                    (0.5, _now(), row["id"]),
                )
        return {"slug": slug, "lifecycle": "archived" if archived else "active",
                "was": row["lifecycle"]}


def restore_skill(conn: sqlite3.Connection, slug: str, *, by: str | None = None) -> bool:
    """Bring a hidden skill back to 'active': ``set_archived(archived=False)``."""
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
    from .embed import DIM
    rows = [r for r in rows if len(r["embedding"]) == DIM * 4]   # see _vector_ids
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
    fetched = _fetch_live(conn, ranked_ids, visible, kind="skill")
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
            # what a caller serving several principals filters on, and approval
            "visibility": row["visibility"],
            "agent": row["agent"],
            "topics": _parse_json_list(row["topics"]),
            "origin": row["origin"],
            "trusted_at": row["trusted_at"],
            "kind": row["kind"],
            "tags": _parse_json_list(row["tags"]),
        }
        if row["body_path"]:
            d["body"] = served_body(MemoryItem.from_row(row))
        results.append(d)
    if auto_reinforce:
        bumped = reinforce_retrieved(conn, [d["slug"] for d in results], visible=visible)
        for d in results:
            if d["slug"] in bumped:
                d.update(strength=bumped[d["slug"]]["strength"],
                         access_count=bumped[d["slug"]]["access_count"])
    return results
