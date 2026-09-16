"""Importing third-party skill packs: parsing, provenance, and the undo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skillmem import packs as P
from skillmem import storage as S
from skillmem.cli import main as cli_main


@pytest.fixture
def conn(tmp_path: Path):
    c = S.connect(tmp_path / "test.db")
    S.init_schema(c)
    return c


@pytest.fixture
def pack_dir(tmp_path: Path) -> Path:
    """A pack shaped like the real ones: SKILL.md files plus noise to ignore."""
    root = tmp_path / "somepack"
    (root / "skills" / "lazy").mkdir(parents=True)
    (root / "skills" / "lazy" / "SKILL.md").write_text(
        "---\n"
        "name: lazy\n"
        "description: >\n"
        "  Forces the simplest solution that works.\n"
        "  Use on any coding task.\n"
        "license: MIT\n"
        "---\n"
        "\n# Lazy\n\nThe best code is the code never written.\n",
        encoding="utf-8",
    )
    (root / "skills" / "deep").mkdir(parents=True)
    (root / "skills" / "deep" / "SKILL.md").write_text(
        "---\nname: deep\ndescription: Splits a task N layers deep.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    # Noise: a test fixture skill and an executable — neither may be imported.
    (root / "evals" / "fixtures" / "x").mkdir(parents=True)
    (root / "evals" / "fixtures" / "x" / "SKILL.md").write_text("nope", encoding="utf-8")
    (root / "install.sh").write_text("#!/bin/sh\nrm -rf /\n", encoding="utf-8")
    (root / "LICENSE").write_text("MIT License\n\nCopyright (c) 2026\n", encoding="utf-8")
    return root


def test_reads_only_skill_files_outside_build_dirs(pack_dir: Path):
    found = {p.parent.name for p in P.iter_skill_files(pack_dir)}
    assert found == {"lazy", "deep"}          # the evals fixture is skipped


def test_frontmatter_folded_description_is_joined(pack_dir: Path):
    skills = {s.name: s for s in P.read_pack(pack_dir)}
    assert skills["lazy"].description == (
        "Forces the simplest solution that works. Use on any coding task."
    )
    assert skills["lazy"].title == "Forces the simplest solution that works"


def test_import_writes_skills_with_provenance(conn, pack_dir: Path):
    report = P.import_pack(conn, str(pack_dir))
    assert sorted(report.imported) == ["pack-somepack-deep", "pack-somepack-lazy"]
    assert report.license == "MIT License"

    item = S.get(conn, "pack-somepack-lazy")
    assert item.kind == "skill"
    assert item.project == "pack:somepack"
    assert "untrusted-origin" in item.tags     # a stranger's instructions, marked
    body = S.load_body(item)
    assert "Imported skill" in body and "MIT License" in body
    assert "skills/lazy/SKILL.md" in body      # which file it came from


def test_imported_skills_are_recallable_and_decay_like_any_other(conn, pack_dir: Path):
    P.import_pack(conn, str(pack_dir))
    hits = S.recall_skills(conn, "simplest solution", auto_reinforce=False)
    assert any(h["slug"] == "pack-somepack-lazy" for h in hits)

    conn.execute("UPDATE memory_items SET last_accessed_at = 1")
    decayed = {d["slug"] for d in S.decay_stale(conn, days_threshold=0)}
    assert "pack-somepack-lazy" in decayed


def test_empty_pack_is_an_error(conn, tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(P.PackError):
        P.import_pack(conn, str(empty))


def test_list_and_remove_pack(conn, pack_dir: Path):
    P.import_pack(conn, str(pack_dir))
    S.reinforce(conn, "pack-somepack-lazy", evidence="test_passed")
    S.reinforce(conn, "pack-somepack-deep", evidence="failure")

    listed = {p["pack"]: p for p in P.list_packs(conn)}
    assert listed["somepack"]["skills"] == 2
    assert listed["somepack"]["confirmed"] == 1
    assert listed["somepack"]["failures"] == 1

    removed = P.remove_pack(conn, "somepack", reason="test")
    assert len(removed) == 2
    assert S.get(conn, "pack-somepack-lazy") is None
    assert P.list_packs(conn) == []


def test_resolve_source_shorthand_and_url():
    assert P.resolve_source("DietrichGebert/ponytail") == (
        "https://github.com/DietrichGebert/ponytail.git", "ponytail")
    url, name = P.resolve_source("https://example.com/foo/bar.git")
    assert url == "https://example.com/foo/bar.git" and name == "bar"


def test_cli_dry_run_imports_nothing(tmp_path: Path, pack_dir: Path):
    db = tmp_path / "cli.db"
    res = CliRunner().invoke(
        cli_main, ["--db", str(db), "skills", "add", str(pack_dir), "--dry-run"],
        catch_exceptions=False,
    )
    assert res.exit_code == 0
    assert "would import 2 skills" in res.output
    payload = json.loads(res.output.split("\n\n")[0])
    assert len(payload["imported"]) == 2

    conn = S.connect(db)
    S.init_schema(conn)
    assert S.get(conn, "pack-somepack-lazy") is None


def test_per_agent_copies_of_one_skill_import_once(conn, tmp_path: Path):
    """Packs ship the same skill per agent format; the canonical copy wins."""
    root = tmp_path / "multi"
    for rel in ("skills/lazy", ".openclaw/skills/lazy", ".agents/skills/lazy"):
        (root / rel).mkdir(parents=True)
        (root / rel / "SKILL.md").write_text(
            f"---\nname: lazy\ndescription: From {rel}.\n---\n\nBody.\n",
            encoding="utf-8")

    report = P.import_pack(conn, str(root))
    assert report.imported == ["pack-multi-lazy"]
    assert "skills/lazy/SKILL.md" in S.load_body(S.get(conn, "pack-multi-lazy"))


def test_remove_pack_leaves_the_users_own_note_in_that_project(conn, pack_dir: Path):
    P.import_pack(conn, str(pack_dir))
    S.upsert(conn, S.MemoryItem(slug="owner-note", kind="note", title="mine", body="my own words",
                                project="pack:somepack", agent="me", origin="owner"))
    removed = P.remove_pack(conn, "somepack", reason="test")
    assert "owner-note" not in removed and len(removed) == 2
    assert S.get(conn, "owner-note") is not None and S.get(conn, "owner-note").deleted_at is None
