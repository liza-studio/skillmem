"""Shared pytest fixtures for skillmem unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def memhome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate SKILLMEM_HOME under tmp so tests never touch the real DB."""
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path))
    # Hook state (recap stamps, parallel slots, logs) lives in the state dir:
    # without this the suite writes into the developer's live ~/.local/state
    # and a stamp left by one test silently debounces the next.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # XDG_STATE_HOME is ignored on Windows — the explicit override is the only
    # isolation that holds on every OS.
    monkeypatch.setenv("SKILLMEM_STATE_DIR", str(tmp_path / "state" / "skillmem"))
    monkeypatch.delenv("SKILLMEM_DB", raising=False)
    return tmp_path


@pytest.fixture
def conn(memhome: Path):
    """A fresh, schema-initialised in-tmp SQLite connection."""
    from skillmem import storage as S
    c = S.connect(memhome / "memory.db")
    S.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def fakehome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend $HOME = tmp dir so `init`/`uninstall` patch synthetic configs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows: Path.home() resolves via USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path / ".skillmem"))
    return tmp_path

@pytest.fixture
def at_terminal(monkeypatch):
    """Pretend a person is at the terminal.

    Writing a record with origin='owner' mints the owner seal, and that is gated
    on a TTY in production: a file an agent can write must not be able to declare
    itself the owner's. pytest has no terminal, so tests that mean "the owner
    typed this" say so with this fixture.
    """
    from skillmem import storage as S
    monkeypatch.setattr(S, "owner_present", lambda: True)
    return True
