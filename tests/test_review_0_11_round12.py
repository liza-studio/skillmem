"""Round-12 findings, 2026-09-18. Two shapes again — a caller-supplied override
around the guard, and a caller path the wall did not name.

Each test PASSES on the parent commit's bug (`2044973`) and FAILS on it after
the matching fix.
"""
from __future__ import annotations

import fnmatch
import inspect
import subprocess
import sys
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


# --- P1: allow_sealed override on soft_delete and set_archived ----------------

def test_soft_delete_has_no_caller_supplied_override_for_the_seal():
    """`soft_delete(..., allow_sealed=True)` let a non-owner delete a sealed
    record on the parent commit: the parameter defaulted to `owner_present()`
    but the caller could pass `True` to skip the check. The mutation must
    read `owner_present()` itself and refuse callers a vote."""
    sig = inspect.signature(S.soft_delete)
    assert "allow_sealed" not in sig.parameters, sig.parameters


def test_set_archived_has_no_caller_supplied_override_for_the_seal():
    """Same shape as soft_delete: `set_archived(..., allow_sealed=True)` was
    the round-12 hole. The check must live inside the transaction and read
    `owner_present()` itself."""
    sig = inspect.signature(S.set_archived)
    assert "allow_sealed" not in sig.parameters, sig.parameters


def test_soft_delete_rejects_a_caller_supplied_seal_override(home, monkeypatch):
    """The reviewer's exact repro: `S.soft_delete(c, slug, reason,
    allow_sealed=True)` on the parent commit deleted a sealed record from a
    caller with no terminal. After the fix the kwarg is not accepted at all,
    so an agent-side caller cannot even reach the code that skips the check.
    """
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="sealed-del", kind="feedback", title="rule",
                                body="the owner's rule, sealed and about to be deleted",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "sealed-del", trusted=True)
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='sealed-del'"
                        ).fetchone()["owner_seal"] == 1
    monkeypatch.setattr(S, "owner_present", lambda: False)
    with pytest.raises(TypeError):
        S.soft_delete(conn, "sealed-del", "agent deletion", allow_sealed=True)
    row = conn.execute("SELECT deleted_at FROM memory_items WHERE slug='sealed-del'").fetchone()
    assert row["deleted_at"] is None


def test_set_archived_rejects_a_caller_supplied_seal_override(home, monkeypatch):
    """Same shape as the delete case: on the parent commit, an agent-side
    caller passing `allow_sealed=True` archived a sealed record. The fix
    removes the parameter."""
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="sealed-arch", kind="feedback", title="rule",
                                body="the owner's rule, sealed and about to be archived",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "sealed-arch", trusted=True)
    assert conn.execute("SELECT owner_seal FROM memory_items WHERE slug='sealed-arch'"
                        ).fetchone()["owner_seal"] == 1
    monkeypatch.setattr(S, "owner_present", lambda: False)
    with pytest.raises(TypeError):
        S.set_archived(conn, "sealed-arch", True, allow_sealed=True)
    row = conn.execute("SELECT lifecycle FROM memory_items WHERE slug='sealed-arch'").fetchone()
    assert row["lifecycle"] == "active"


# --- P1: `python -m skillmem.cli <verb>` caught by the deny rules -------------

def test_python_m_skillmem_cli_entry_point_is_denied():
    """`python -m skillmem.cli trust x` is a supported entry point, and its
    command line reads `... skillmem.cli trust x` — no space between "skillmem"
    and the verb, so the `*skillmem trust*` glob missed it. The `.cli` rules
    close the gap for the four owner commands.
    """
    from skillmem.cli import _OWNER_DENY_RULES

    def denied(cmd: str) -> bool:
        for rule in _OWNER_DENY_RULES:
            pat = rule[len("Bash("):-1]
            if fnmatch.fnmatchcase(cmd, pat):
                return True
        return False

    # The exact forms the reviewer reproduced.
    assert denied("./.venv/bin/python -m skillmem.cli trust foo")
    assert denied("./.venv/bin/python -m skillmem.cli skills-archive foo")
    assert denied("./.venv/bin/python -m skillmem.cli rm foo --reason cleanup")
    assert denied("./.venv/bin/python -m skillmem.cli import-vault /tmp/v")
    # And wrapped in pty helpers — same pattern.
    assert denied(
        "script -qec './.venv/bin/python -m skillmem.cli skills-archive crown' /dev/null"
    )
    assert denied(
        "script -qec 'python -m skillmem.cli rm foo --reason x' /dev/null"
    )


# --- P1: import-vault at the CLI needs a terminal -----------------------------

def test_cli_import_vault_refuses_without_a_terminal(home, monkeypatch, tmp_path):
    """`import-vault` is one of the four owner-only verbs the deny rules list.
    Without a terminal, a forged .md file with `metadata.node_type: memory`
    can revive an owner-deleted sealed slug with replacement text; the
    reviewer's evidence walked exactly that path."""
    monkeypatch.setenv("SKILLMEM_HOME", str(home))
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("---\nname: n\n---\nbody\n", encoding="utf-8")
    # pytest has no TTY; owner_present() is False, and `stdin=DEVNULL` keeps
    # it that way for the child too.
    res = subprocess.run(
        [sys.executable, "-m", "skillmem.cli", "import-vault", str(vault)],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode != 0, res.stdout
    assert "terminal" in (res.stderr + res.stdout).lower()


def test_storage_refuses_to_revive_a_sealed_tombstone_without_owner_call(home, monkeypatch):
    """The mutation layer's own guard, so a caller that avoids the CLI does
    not slip past. `_run_import` used to call
    `upsert(..., revive=True, owner_call=S.owner_present())` and let a
    forged dump resurrect an owner-deleted sealed rule with agent text."""
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="revive-me", kind="feedback", title="Original",
                                body="the owner-approved body of this rule",
                                origin="owner"), owner_call=True)
    S.set_trust(conn, "revive-me", trusted=True)
    # the owner deletes it (with a terminal, using the mutation guard)
    monkeypatch.setattr(S, "owner_present", lambda: True)
    assert S.soft_delete(conn, "revive-me", "owner deleted") is True
    # No terminal — an unattended importer must not revive it.
    monkeypatch.setattr(S, "owner_present", lambda: False)
    with pytest.raises(S.SealedRecord):
        S.upsert(conn, S.MemoryItem(slug="revive-me", kind="feedback",
                                    title="forged replacement",
                                    body="text supplied by unattended importer",
                                    origin="agent"),
                 revive=True, force=True, owner_call=False, reason="vault import")
    row = conn.execute("SELECT title, body, deleted_at FROM memory_items "
                       "WHERE slug='revive-me'").fetchone()
    # the record stays a tombstone with the owner's text preserved in history
    assert row["deleted_at"] is not None
    assert row["title"] == "Original"


# --- P2: `rm` at the CLI needs a terminal -------------------------------------

def test_cli_rm_refuses_without_a_terminal(home, monkeypatch):
    """`rm` was terminal-gated only for sealed rows, so every unsealed record
    (agent-written notes and skills) was deletable through Bash without a
    person at the keyboard. `rm` is one of the four owner-only verbs; the
    gate is accident protection to match `skills-archive` and `trust`."""
    monkeypatch.setenv("SKILLMEM_HOME", str(home))
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="disposable", kind="note", title="d",
                                body="agent text, unsealed and about to be rm'd"))
    conn.commit(); conn.close()
    res = subprocess.run(
        [sys.executable, "-m", "skillmem.cli", "rm", "disposable",
         "--reason", "unattended"],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode != 0, res.stdout
    assert "terminal" in (res.stderr + res.stdout).lower()
    # And the row is untouched.
    check = _conn(home)
    row = check.execute("SELECT deleted_at FROM memory_items "
                        "WHERE slug='disposable'").fetchone()
    assert row["deleted_at"] is None
