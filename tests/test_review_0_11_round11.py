"""Round-11 findings, 2026-09-18. Three P1s reachable without a terminal.

Each test PASSES on the parent commit's bug (`64cc3ce`) and FAILS after the
matching fix — same shape as the earlier round files.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

from skillmem import storage as S


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path))
    monkeypatch.delenv("SKILLMEM_DB", raising=False)
    return tmp_path


def _conn(home: Path, name: str = "memory.db"):
    conn = S.connect(home / name)
    S.init_schema(conn)
    return conn


def test_same_text_owner_write_needs_a_terminal_to_seal(home):
    """The insert branch checks `owner_present()` before minting the seal on
    `origin='owner'` — a file an agent can write reaches that path too. The
    same-text branch used to mint the seal from `origin` alone, so a non-TTY
    vault import over an existing agent record could seal it. Non-TTY delete
    was then refused, and the record stayed invisible behind archived state.
    """
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="agent-first", kind="skill", title="rule",
                                body="the exact body agent-first wrote here",
                                origin="agent"))
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='agent-first'"
                        ).fetchone()["owner_seal"] == 0
    # No terminal — a same-text write claiming origin='owner' must NOT mint
    # the seal. pytest has no TTY, so `owner_present()` returns False on its
    # own, no monkeypatch needed.
    S.upsert(conn, S.MemoryItem(slug="agent-first", kind="skill", title="rule",
                                body="the exact body agent-first wrote here",
                                origin="owner"),
             explicit={"kind"})
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='agent-first'"
                        ).fetchone()["owner_seal"] == 0


def test_seal_backfill_runs_once_and_leaves_later_agent_writes_alone(home):
    """The migration used to run on every open: any row with origin='owner'
    and owner_seal=0 was retroactively sealed. But an agent write without a
    terminal legitimately leaves that state (upsert saw `owner_present()`
    False, so it did not mint), and a later open then sealed it — closing
    every guard against delete and lifecycle archival.

    The backfill records completion in `meta` and never repeats after that.
    """
    conn = _conn(home)
    # A row an agent wrote without a terminal, then forced to origin='owner'
    # the way _migrate_origin (default_origin='owner') does through a non-TTY
    # `skillmem import-vault`. The insert branch left owner_seal=0.
    S.upsert(conn, S.MemoryItem(slug="ghost", kind="skill", title="ghost",
                                body="an agent-written record that could sit here",
                                origin="agent"))
    conn.execute("UPDATE memory_items SET origin = 'owner' WHERE slug = 'ghost'")
    conn.commit()
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='ghost'"
                        ).fetchone()["owner_seal"] == 0
    conn.close()
    # Reopen — the migration must not retro-seal this row.
    reopened = _conn(home)
    assert reopened.execute("SELECT owner_seal FROM memory_items WHERE slug='ghost'"
                            ).fetchone()["owner_seal"] == 0
    reopened.close()


def test_seal_backfill_still_seals_a_legitimately_v10_row_on_first_open(home):
    """The one-time backfill is what makes v10 databases carry the seal on
    every row the owner wrote or approved before v11. That must still run
    on the FIRST open — this test pins that side of the change.
    """
    # Build a v10-era row by hand: origin='owner' and no owner_seal column.
    import sqlite3
    db = home / "v10.db"
    conn = S.connect(db)
    S.init_schema(conn)
    # Simulate a pre-owner_seal database: drop the column marker and unseal a row.
    S.upsert(conn, S.MemoryItem(slug="pre-v11", kind="feedback", title="rule",
                                body="an owner-written rule from a pre-v11 DB",
                                origin="owner"))
    # Wipe the seal and the completion marker, mimicking a fresh v10 file.
    conn.execute("UPDATE memory_items SET owner_seal = 0 WHERE slug = 'pre-v11'")
    conn.execute("DELETE FROM meta WHERE key = 'owner_seal_backfill_done'")
    conn.commit()
    conn.close()
    # Reopen — the backfill runs because completion was never recorded.
    fresh = S.connect(db)
    S.init_schema(fresh)
    assert fresh.execute("SELECT owner_seal FROM memory_items WHERE slug='pre-v11'"
                         ).fetchone()["owner_seal"] == 1
    fresh.close()


def test_deny_rules_catch_pty_wrappers_around_the_owner_commands():
    """`skillmem trust*` alone reads as a command starting with `skillmem`.
    A wrap like `script -qec 'skillmem trust x' /dev/null` starts with
    `script`, so Claude Code's prefix match missed it and the owner commands
    were reachable through any pty helper (script, unbuffer, expect, socat,
    a Python one-liner using pty.spawn). The four verbs must be denied
    wherever they appear in the command line.
    """
    from skillmem.cli import _OWNER_DENY_RULES

    def denied(cmd: str) -> bool:
        for rule in _OWNER_DENY_RULES:
            pat = rule[len("Bash("):-1]
            if fnmatch.fnmatchcase(cmd, pat):
                return True
        return False

    # Direct invocations — the original prefix rules keep matching.
    assert denied("skillmem trust foo")
    assert denied("skillmem skills-archive foo")
    assert denied("skillmem rm foo --reason cleanup")
    assert denied("skillmem import-vault /tmp/v")
    # `script -qec 'skillmem trust foo' /dev/null` and similar wrappers.
    assert denied("script -qec 'skillmem trust foo' /dev/null")
    assert denied("script -qec './.venv/bin/skillmem trust foo' /dev/null")
    assert denied("script -qec 'skillmem skills-archive foo' /dev/null")
    assert denied("script -qec 'skillmem rm foo' /dev/null")
    assert denied("script -qec 'skillmem import-vault /tmp/v' /dev/null")
    assert denied("unbuffer skillmem trust foo")
    assert denied("expect -c 'spawn skillmem trust foo; interact'")
    # And a command that has no business being denied is still allowed.
    assert not denied("skillmem write --slug x --title t --body b --kind note")
    assert not denied("skillmem search rule")
    assert not denied("git status")
