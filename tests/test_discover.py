"""Discovery of Claude Code memory dirs, tolerant of broken symlinks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from skillmem.migrate import discover_claude_memory_dirs


def test_discover_returns_empty_when_no_projects(tmp_path: Path):
    assert discover_claude_memory_dirs(home=tmp_path) == []


def test_discover_finds_multiple(tmp_path: Path):
    projects = tmp_path / ".claude" / "projects"
    for name in ("proj-a", "proj-b", "proj-c"):
        (projects / name / "memory").mkdir(parents=True)
    found = discover_claude_memory_dirs(home=tmp_path)
    assert len(found) == 3
    assert all(p.name == "memory" for p in found)


def test_discover_skips_dead_symlink(tmp_path: Path):
    projects = tmp_path / ".claude" / "projects"
    (projects / "good" / "memory").mkdir(parents=True)
    # A dead symlink where memory should be
    bad = projects / "broken"
    bad.mkdir()
    try:
        os.symlink("/nonexistent/path/12345", bad / "memory")
    except OSError:
        pytest.skip("symlinks require elevated privileges on this platform")
    found = discover_claude_memory_dirs(home=tmp_path)
    assert len(found) == 1
    assert "good" in str(found[0])
