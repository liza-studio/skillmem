"""Obsidian vault import: wikilinks -> mem_links, asset dedup, idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from skillmem import storage as S
from skillmem.vault import import_vault


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # fake but valid-enough asset


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "note-a.md").write_text(
        "# Note A\n\nSee [[note-b]] and [[note-c|the third one]].\n\n![[pic.png]]\n",
        encoding="utf-8",
    )
    (root / "note-b.md").write_text(
        "Links back to [[note-a]].\n\n![[dup.png]]\n", encoding="utf-8"
    )
    (root / "note-c.md").write_text(
        "---\ntitle: Note C\n---\nBody of C, mentions [[note-a]].\n",
        encoding="utf-8",
    )
    sub = root / "projectx"
    sub.mkdir()
    (sub / "note-d.md").write_text("Nested note, no links.\n", encoding="utf-8")
    # Two differently-named files with IDENTICAL bytes -> must dedup to one asset.
    (root / "pic.png").write_bytes(PNG_BYTES)
    (root / "dup.png").write_bytes(PNG_BYTES)
    return root


def _asset_files(memhome: Path) -> list[Path]:
    assets = memhome / "assets"
    return sorted(p for p in assets.rglob("*") if p.is_file()) if assets.exists() else []


def test_import_vault_basics(conn, memhome: Path, vault: Path):
    report = import_vault(conn, vault)
    assert report.failed == []
    assert report.inserted == 4
    assert report.updated == 0

    a = S.get(conn, "note-a")
    assert a is not None and a.kind == "document"
    assert a.title == "Note A"  # from the heading
    c = S.get(conn, "note-c")
    assert c.title == "Note C"  # from loose frontmatter

    # Project derived from the top-level folder for nested notes only.
    d = S.get(conn, "projectx-note-d")
    assert d is not None and d.project == "projectx"
    assert a.project is None


def test_wikilinks_become_mem_links(conn, vault: Path):
    import_vault(conn, vault)
    # Note: the attachment embed ![[pic.png]] also matches the wikilink regex,
    # so the asset name shows up as a link target — pinned actual behavior.
    assert set(S.links_from(conn, "note-a")) == {"note-b", "note-c", "pic.png"}
    assert set(S.links_to(conn, "note-a")) == {"note-b", "note-c"}


def test_assets_deduped_by_content_hash(conn, memhome: Path, vault: Path):
    import_vault(conn, vault)
    files = _asset_files(memhome)
    # pic.png and dup.png are byte-identical -> exactly one stored file,
    # named by its sha256 digest.
    assert len(files) == 1
    digest_name = files[0].name
    assert digest_name.endswith(".png") and len(digest_name) == 64 + 4

    a = S.get(conn, "note-a")
    b = S.get(conn, "note-b")
    assert a.attachments == b.attachments
    assert len(a.attachments) == 1
    assert a.attachments[0].endswith(digest_name)


def test_reimport_is_idempotent(conn, memhome: Path, vault: Path):
    import_vault(conn, vault)
    count_before = conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0]
    links_before = conn.execute("SELECT COUNT(*) FROM mem_links").fetchone()[0]
    history_before = conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0]
    assets_before = len(_asset_files(memhome))

    report = import_vault(conn, vault)
    assert report.failed == []
    assert report.inserted == 0
    assert report.updated == 4  # counted as updated, but content is unchanged

    assert conn.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == count_before
    assert conn.execute("SELECT COUNT(*) FROM mem_links").fetchone()[0] == links_before
    # Unchanged content short-circuits before writing history — no version spam.
    assert conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] == history_before
    assert len(_asset_files(memhome)) == assets_before


def test_skip_auto_memories_flag(conn, tmp_path: Path):
    root = tmp_path / "vault2"
    root.mkdir()
    (root / "auto.md").write_text(
        "---\nname: auto-note\nmetadata:\n  node_type: memory\n  type: feedback\n---\nbody\n",
        encoding="utf-8",
    )
    report = import_vault(conn, root, skip_auto_memories=True)
    assert report.skipped == 1 and report.inserted == 0
    report = import_vault(conn, root, skip_auto_memories=False)
    assert report.inserted == 1
    assert S.get(conn, "auto-note").kind == "feedback"  # metadata.type wins over default
