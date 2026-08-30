"""Obsidian vault importer (LML §10.1 day 3).

Recursively walks a directory tree, imports every .md as a memory of
``kind=document`` by default, derives the ``project`` tag from the top-level
folder name, copies attached images / PDFs to the assets directory, and
resolves ``[[wikilinks]]`` into ``mem_links``.

The migrator from ``migrate.py`` handles single .md files with frontmatter
shaped like Claude Code auto-memory; this module handles arbitrary trees with
no frontmatter or with loose Obsidian frontmatter.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .migrate import desurrogate

import yaml

from . import storage as S
from .migrate import FRONTMATTER_RE


SUPPORTED_TEXT = {".md", ".markdown"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".svg"}

_SLUG_TRIM = re.compile(r"[^a-z0-9а-яё\-]+", re.IGNORECASE)


def _slug_from_meta_or_path(meta: dict, root: Path, path: Path) -> str:
    """Prefer frontmatter ``name:`` so an export/import round-trip is lossless."""
    raw = meta.get("name")
    if raw:
        slug = _SLUG_TRIM.sub("-", str(raw).lower())
    else:
        rel = path.relative_to(root).with_suffix("")
        joined = "-".join(p.lower() for p in rel.parts)
        slug = _SLUG_TRIM.sub("-", joined)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug or "untitled"


def _project_from_path(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root)
    parts = rel.parts
    if len(parts) > 1:
        return parts[0]
    return None


def _parse_md(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    raw, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    # Same surrogate hazard as migrate.parse_file — see migrate.desurrogate.
    return desurrogate(meta), desurrogate(body.strip())


def _is_auto_memory(meta: dict) -> bool:
    md = meta.get("metadata") or {}
    if isinstance(md, dict) and md.get("node_type") == "memory":
        return True
    return False


def _title_from(meta: dict, body: str, fallback: str) -> str:
    for key in ("title", "description", "name"):
        v = meta.get(key)
        if v:
            return str(v).strip()
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:120]
    return fallback


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _store_asset(asset: Path, assets_root: Path) -> str:
    digest = _hash_file(asset)
    sub = assets_root / digest[:2]
    sub.mkdir(parents=True, exist_ok=True)
    dest = sub / (digest + asset.suffix.lower())
    if not dest.exists():
        shutil.copy2(asset, dest)
    return str(dest.relative_to(assets_root.parent))


_ATTACHMENT_RE = re.compile(r"!\[\[([^\]\n]+?)\]\]")


def _collect_attachments(root: Path, current_dir: Path, body: str) -> list[Path]:
    out: list[Path] = []
    for match in _ATTACHMENT_RE.finditer(body):
        target = match.group(1).split("|", 1)[0].strip()
        if not target:
            continue
        # Obsidian resolves attachments either next to the note or anywhere
        # in the vault; we try both, prefer adjacent.
        for candidate in (current_dir / target, *root.rglob(target)):
            if candidate.exists() and candidate.suffix.lower() in ASSET_EXTS:
                out.append(candidate)
                break
    return out


@dataclass
class VaultReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = None

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


def _iter_md(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_TEXT:
            yield path


def import_vault(
    conn,
    root: Path,
    *,
    kind: str = "document",
    project_override: str | None = None,
    skip_auto_memories: bool = True,
) -> VaultReport:
    """Bulk-import an Obsidian vault. All upserts run inside a single outer
    transaction so a 1000-file vault is one commit, not 1000 (with per-file
    SAVEPOINTs so individual failures still roll back cleanly)."""
    report = VaultReport()
    root = Path(root)
    assets_root = S.default_data_dir() / "assets"

    with S.tx(conn):
        _run_import(conn, root, assets_root, kind, project_override,
                    skip_auto_memories, report)
    return report


def _run_import(conn, root, assets_root, kind, project_override,
                skip_auto_memories, report) -> None:
    for path in _iter_md(root):
        try:
            meta, body = _parse_md(path.read_text(encoding="utf-8"))
            if skip_auto_memories and _is_auto_memory(meta):
                report.skipped += 1
                continue
            slug = _slug_from_meta_or_path(meta, root, path)
            project = project_override or _project_from_path(root, path)
            title = _title_from(meta, body, slug)
            md = meta.get("metadata") or {}
            item_kind = kind
            if isinstance(md, dict) and md.get("type"):
                item_kind = str(md["type"])

            attachments: list[str] = []
            for asset in _collect_attachments(root, path.parent, body):
                attachments.append(_store_asset(asset, assets_root))

            item = S.MemoryItem(
                slug=slug,
                kind=item_kind,
                title=title,
                body=body,
                project=project,
                attachments=attachments,
                visibility="private",
            )
            existed = conn.execute(
                "SELECT 1 FROM memory_items WHERE slug = ?", (slug,)
            ).fetchone()
            S.upsert(
                conn, item,
                reason="vault import" if existed else None,
                force=True,
                links=S.extract_wikilinks(body),
            )
            if existed:
                report.updated += 1
            else:
                report.inserted += 1
        except Exception as exc:  # noqa: BLE001
            report.failed.append((str(path.relative_to(root)), repr(exc)))
