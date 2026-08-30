"""Dump every memory back to .md with YAML frontmatter.

Vendor-lock defense: if skillmem ever dies, you keep your data as
ordinary markdown files. The export is round-trip-safe — re-importing the dump
via ``vault.import_vault`` yields the same slug/kind/title/body plus metadata
(project/tags/topics/visibility/agent/strength/ttl/freshness).
"""

from __future__ import annotations

import datetime as dt
import re
import time
from pathlib import Path
from typing import Iterable

import yaml

from . import storage as S


_SAFE_FN = re.compile(r"[^\w.\-]+", re.UNICODE)


def _safe_filename(slug: str) -> str:
    name = _SAFE_FN.sub("-", slug).strip("-")
    return name or "untitled"


def _frontmatter(item: S.MemoryItem, *, truncated: bool = False) -> str:
    meta = {
        "name": item.slug,
        "description": item.title,
        "metadata": {
            "node_type": "memory",
            "type": item.kind,
            "originSessionId": item.source_session,
        },
        "exported_at": dt.datetime.fromtimestamp(int(time.time()), tz=dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    if item.project:
        meta["project"] = item.project
    if item.tags:
        meta["tags"] = item.tags
    if item.topics:
        meta["topics"] = item.topics
    if item.agent:
        meta["agent"] = item.agent
    if item.ttl_days:
        meta["ttl_days"] = item.ttl_days
    if item.freshness_until:
        meta["freshness_until"] = item.freshness_until
    if item.visibility and item.visibility != "private":
        meta["visibility"] = item.visibility
    if item.strength != 1.0:
        meta["strength"] = item.strength
    if truncated:
        # The externalized body file was lost; only the excerpt follows.
        # Without this marker the dump would look complete while being partial.
        meta["truncated"] = True
    return yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()


def _iter_all(conn) -> Iterable[S.MemoryItem]:
    rows = conn.execute(
        "SELECT * FROM memory_items WHERE deleted_at IS NULL ORDER BY kind, slug"
    ).fetchall()
    return (S.MemoryItem.from_row(r) for r in rows)


def export_all(conn, destination: Path) -> int:
    """Write every memory as ``<destination>/<kind>/<slug>.md``. Returns count."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for item in _iter_all(conn):
        folder = destination / item.kind
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{_safe_filename(item.slug)}.md"
        truncated = bool(item.body_path) and not (S.docs_dir() / item.body_path).exists()
        body = S.load_body(item)  # full body even for externalized docs
        content = ("---\n" + _frontmatter(item, truncated=truncated)
                   + "\n---\n\n" + body.strip() + "\n")
        path.write_text(content, encoding="utf-8")
        count += 1
    return count
