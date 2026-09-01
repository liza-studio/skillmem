"""Markdown export round-trip (the "no vendor lock" promise from README).

`export_all` dumps every memory as ``<dest>/<kind>/<slug>.md`` with YAML
frontmatter; re-importing the dump must yield the same slug/kind/title/body
AND metadata — project/tags/topics/visibility/agent/strength/ttl/freshness
(the contract stated in export.py's docstring).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from skillmem import storage as S
from skillmem.export import export_all
from skillmem.vault import import_vault


# Comfortably over the 8 KB externalization threshold, bilingual on purpose.
BIG_BODY = ("Deployment checklist и резервная копия базы данных перед рестартом. " * 200).strip()

EN_BODY = (
    "Steps to deploy: backup DB first, then restart via script.\n\n"
    "Related: [[skill-restart]] and [[ref-monitoring]]."
)
RU_BODY = (
    "Бэкап делается каждую ночь, восстановление проверяем раз в месяц.\n\n"
    "См. также [[ref-deploy-en]]."
)


# Pristine copies for round-trip comparison: S.upsert mutates item.body to the
# excerpt when a body is externalized, so the MemoryItem objects can't serve as
# the expected values.
EXPECTED = {
    "ref-deploy-en": ("reference", "Deploy checklist (EN)", EN_BODY),
    "заметка-про-бэкап": ("note", "Бэкап и восстановление", RU_BODY),
    "doc-big": ("document", "Big externalized document", BIG_BODY),
}


def _seed(conn) -> dict[str, S.MemoryItem]:
    items = {
        "ref-deploy-en": S.MemoryItem(
            slug="ref-deploy-en", kind="reference",
            title="Deploy checklist (EN)", body=EN_BODY,
            project="liza", tags=["deploy", "ops"], topics=["infra"],
            visibility="public", strength=1.5,
        ),
        "заметка-про-бэкап": S.MemoryItem(
            slug="заметка-про-бэкап", kind="note",
            title="Бэкап и восстановление", body=RU_BODY,
        ),
        "doc-big": S.MemoryItem(
            slug="doc-big", kind="document",
            title="Big externalized document", body=BIG_BODY,
        ),
    }
    for item in items.values():
        S.upsert(conn, item, links=S.extract_wikilinks(item.body))
    return items


def test_export_writes_per_kind_tree_with_frontmatter(conn, memhome: Path):
    _seed(conn)
    dest = memhome / "dump"
    assert export_all(conn, dest) == 3

    en = dest / "reference" / "ref-deploy-en.md"
    ru = dest / "note" / "заметка-про-бэкап.md"
    big = dest / "document" / "doc-big.md"
    assert en.exists() and ru.exists() and big.exists()

    raw = en.read_text(encoding="utf-8")
    assert raw.startswith("---\n")
    meta = yaml.safe_load(raw.split("---\n")[1])
    assert meta["name"] == "ref-deploy-en"
    assert meta["description"] == "Deploy checklist (EN)"
    assert meta["metadata"]["node_type"] == "memory"
    assert meta["metadata"]["type"] == "reference"
    assert meta["project"] == "liza"
    assert meta["tags"] == ["deploy", "ops"]
    assert meta["topics"] == ["infra"]
    assert meta["visibility"] == "public"
    assert meta["strength"] == 1.5
    assert "truncated" not in meta

    # Defaults are NOT written: private/1.0 items stay clean markdown.
    meta_ru = yaml.safe_load(ru.read_text(encoding="utf-8").split("---\n")[1])
    assert "visibility" not in meta_ru
    assert "strength" not in meta_ru

    # Externalized body must be exported in FULL, not just the DB excerpt.
    assert BIG_BODY in big.read_text(encoding="utf-8")


def test_round_trip_preserves_slug_kind_title_body(conn, memhome: Path, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    _seed(conn)
    dest = memhome / "dump"
    export_all(conn, dest)

    # Import into a completely separate SKILLMEM_HOME + DB.
    home2 = tmp_path / "home2"
    monkeypatch.setenv("SKILLMEM_HOME", str(home2))
    conn2 = S.connect(home2 / "memory.db")
    S.init_schema(conn2)
    try:
        report = import_vault(conn2, dest, skip_auto_memories=False)
        assert report.failed == []
        assert report.inserted == 3

        for slug, (kind, title, body) in EXPECTED.items():
            got = S.get(conn2, slug)
            assert got is not None, f"{slug} lost in round-trip"
            assert got.kind == kind
            assert got.title == title
            assert S.load_body(got).strip() == body.strip()

        # Wikilinks are re-extracted from the body on import.
        assert "skill-restart" in S.links_from(conn2, "ref-deploy-en")
        assert "ref-deploy-en" in S.links_from(conn2, "заметка-про-бэкап")

        # Full metadata round-trip: everything the exporter writes to the
        # frontmatter is rehydrated by the vault importer.
        got_en = S.get(conn2, "ref-deploy-en")
        assert got_en.tags == ["deploy", "ops"]
        assert got_en.topics == ["infra"]
        assert got_en.project == "liza"        # frontmatter beats kind folder
        assert got_en.visibility == "public"
        assert got_en.strength == 1.5

        # An item without project/tags/etc. comes back with the defaults —
        # the kind folder of an exported dump is NOT mistaken for a project.
        got_ru = S.get(conn2, "заметка-про-бэкап")
        assert got_ru.project is None
        assert got_ru.tags == []
        assert got_ru.visibility == "private"
        assert got_ru.strength == 1.0
    finally:
        conn2.close()


def test_export_with_missing_body_file_warns_but_writes_excerpt(
    conn, memhome: Path, caplog: pytest.LogCaptureFixture
):
    """A vanished body file must not be exported SILENTLY as a full document.

    storage.load_body logs a WARNING and falls back to the stored excerpt, so
    the export completes and the operator sees the warning in the log; the
    exported .md itself carries ``truncated: true`` in the frontmatter so the
    backup does not masquerade as complete.
    """
    saved = S.upsert(conn, S.MemoryItem(slug="doc-lost", kind="document",
                                        title="Doomed doc", body=BIG_BODY))
    assert saved.body_path, "test premise: body must be externalized"
    (S.docs_dir() / saved.body_path).unlink()

    dest = memhome / "dump"
    with caplog.at_level(logging.WARNING, logger="skillmem.storage"):
        assert export_all(conn, dest) == 1

    assert "body file missing" in caplog.text, "silent truncated export!"
    exported = (dest / "document" / "doc-lost.md").read_text(encoding="utf-8")
    # Only the excerpt made it out — the full body is genuinely gone.
    assert len(exported) < len(BIG_BODY)
    assert "…" in exported
    # And the file says so explicitly, not only the log.
    meta = yaml.safe_load(exported.split("---\n")[1])
    assert meta.get("truncated") is True


def test_export_no_filename_collision(tmp_path, conn):
    """Slugs that sanitise identically must land in distinct files."""
    S.upsert(conn, S.MemoryItem(slug="a/b", title="one", body="body one"))
    S.upsert(conn, S.MemoryItem(slug="a-b", title="two", body="body two"))
    n = export_all(conn, tmp_path / "dump")
    files = list((tmp_path / "dump").rglob("*.md"))
    assert n == 2
    assert len(files) == 2, "sanitised filenames collided; one record was overwritten"


def test_search_hits_carry_no_embedding_blob(conn):
    """The raw float32 blob must never leak into search hits (CLI json / HTTP)."""
    S.upsert(conn, S.MemoryItem(slug="emb-row", title="deploy nginx", body="steps"))
    conn.execute(
        "UPDATE memory_items SET embedding = ? WHERE slug = 'emb-row'",
        (b"\x00" * 1536,),
    )
    hits = S.search(conn, "deploy nginx")
    assert hits, "row should be found via BM25"
    assert "embedding" not in hits[0]
