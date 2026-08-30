"""Shared pytest fixtures for skillmem unit tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def memhome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate SKILLMEM_HOME under tmp so tests never touch the real DB."""
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path))
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
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path / ".skillmem"))
    return tmp_path
