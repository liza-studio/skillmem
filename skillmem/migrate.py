"""Import Claude Code auto-memory .md files into skillmem.

Frontmatter shape we see in the wild:

    ---
    name: feedback-no-hallucinations
    description: "..."
    metadata:
      node_type: memory
      type: feedback
      originSessionId: 70da1c82-...
    ---
    body...

Some files have looser frontmatter; we fall back gracefully.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import storage as _storage
from .storage import MemoryItem, extract_wikilinks, upsert


def _resolve_default_source() -> Path:
    """Auto-resolve the auto-memory dir per machine. No hardcoded paths.

    Priority: $SKILLMEM_SOURCE_DIR → first ~/.claude/projects/*/memory →
    ~/.claude/projects (safe fallback — never raises, just returns nothing).
    """
    env = os.environ.get("SKILLMEM_SOURCE_DIR")
    if env:
        return Path(env)
    root = Path.home() / ".claude" / "projects"
    if root.exists():
        try:
            found = sorted(root.glob("*/memory"))
        except (OSError, PermissionError):
            found = []
        if found:
            return found[0]
    return root


DEFAULT_SOURCE_DIR = _resolve_default_source()


def discover_claude_memory_dirs(home: Path | None = None) -> list[Path]:
    """Find every ``~/.claude/projects/*/memory`` directory on this machine.

    Skips dead symlinks and permission-denied entries (logged), so a single
    bad dir doesn't take down the whole discovery.
    """
    import logging as _logging
    log = _logging.getLogger("skillmem.migrate")
    home = home or Path.home()
    projects_root = home / ".claude" / "projects"
    if not projects_root.exists():
        return []
    out: list[Path] = []
    try:
        candidates = list(projects_root.glob("*/memory"))
    except PermissionError as exc:
        log.warning("cannot list %s: %s", projects_root, exc)
        return []
    for p in candidates:
        try:
            if p.is_dir():
                out.append(p)
        except (OSError, PermissionError) as exc:
            log.warning("skipping %s: %s", p, exc)
    return sorted(out)

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


@dataclass
class ImportReport:
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    failed: list[tuple[str, str]] = None  # (file, error)

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


def desurrogate(value: Any) -> Any:
    """Repair lone/paired UTF-16 surrogates left behind by YAML \\uXXXX escapes.

    Frontmatter written as a JSON-escaped scalar (``json.dumps`` with the
    default ``ensure_ascii=True``) encodes an emoji as an escaped surrogate
    PAIR. PyYAML decodes each half into a separate lone surrogate instead of
    recombining them, and the resulting str cannot be encoded back to UTF-8 —
    every downstream write raises ``UnicodeEncodeError: surrogates not
    allowed`` and the record is lost. Recombine valid pairs, drop the rest.
    """
    if isinstance(value, str):
        if not any("\ud800" <= ch <= "\udfff" for ch in value):
            return value
        # 'surrogatepass' lets the pair round-trip through UTF-16, which joins it
        # back into the real character; anything still broken is dropped.
        try:
            return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except UnicodeError:
            return "".join(ch for ch in value if not "\ud800" <= ch <= "\udfff")
    if isinstance(value, dict):
        return {desurrogate(k): desurrogate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [desurrogate(v) for v in value]
    return value


def parse_file(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text.strip()
    raw_fm, body = match.group(1), match.group(2)
    try:
        meta = yaml.safe_load(raw_fm) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return desurrogate(meta), desurrogate(body.strip())


def _slug_from(meta: dict[str, Any], path: Path) -> str:
    raw = meta.get("name") or path.stem
    raw = str(raw).strip().lower()
    raw = re.sub(r"[^a-z0-9а-яё\-]+", "-", raw, flags=re.IGNORECASE)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    return raw or path.stem


def _kind_from(meta: dict[str, Any], path: Path) -> str:
    md = meta.get("metadata") or {}
    if isinstance(md, dict):
        t = md.get("type")
        if t:
            return str(t)
    prefix = path.stem.split("_", 1)[0]
    if prefix in {"feedback", "project", "reference", "user"}:
        return prefix
    return "note"


def _title_from(meta: dict[str, Any], body: str, slug: str) -> str:
    desc = meta.get("description")
    if desc:
        return str(desc).strip()
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
        if line:
            return line[:120]
    return slug


# A file can claim anything in its frontmatter, so a claimed origin is accepted
# only when it is NOT a claim of ownership: re-importing a file must never be a
# way to launder an imported or model-written memory into a trusted one. Trust
# itself is never imported — only the owner grants it.
_CLAIMABLE_ORIGINS = ("agent", "imported", "derived")


def _origin_from(meta: dict[str, Any], kind: str, default: str = "owner") -> str:
    """The caller declares provenance; the file may only lower it, never raise it.

    `default` is what the importer knows about the directory it is reading (the
    owner's own memory dir → owner). A file claiming `origin: owner` is ignored,
    or re-importing a pack would be a way to launder it into a trusted rule.
    """
    md = meta.get("metadata") or {}
    claimed = str(md.get("origin") or "").strip().lower() if isinstance(md, dict) else ""
    if claimed in _CLAIMABLE_ORIGINS:
        return claimed
    # A session recap is a model's summary of a transcript, whatever the file says.
    return "derived" if kind == "note" else default


def _source_session(meta: dict[str, Any]) -> str | None:
    md = meta.get("metadata") or {}
    if isinstance(md, dict):
        # source_session is what the recap hook writes; the other two come from
        # older exports. Missing it left every session note unlinked.
        sid = (md.get("source_session") or md.get("originSessionId")
               or md.get("sessionId"))
        if sid:
            return str(sid)
    return None


def import_file(conn, path: Path, *, force: bool = True,
                default_origin: str = "agent") -> str:
    """Import a single .md file. Returns 'inserted' | 'updated'."""
    meta, body = parse_file(path)
    slug = _slug_from(meta, path)
    kind = _kind_from(meta, path)
    title = _title_from(meta, body, slug)

    existed = conn.execute(
        "SELECT id FROM memory_items WHERE slug = ?", (slug,)
    ).fetchone()

    item = MemoryItem(
        slug=slug,
        kind=kind,
        title=title,
        body=body,
        source_session=_source_session(meta),
        visibility="private",
        origin=_origin_from(meta, kind, default_origin),
    )
    upsert(
        conn, item,
        reason="migrated from .md" if existed else None,
        force=force,
        links=extract_wikilinks(body),
    )
    return "updated" if existed else "inserted"


def import_dir(
    conn,
    source: Path = DEFAULT_SOURCE_DIR,
    *,
    skip_index: bool = True,
    default_origin: str = "agent",
) -> ImportReport:
    report = ImportReport()
    if not source.exists():
        report.failed.append((str(source), "directory not found"))
        return report

    with _storage.tx(conn):
        for path in sorted(source.glob("*.md")):
            if skip_index and path.name.upper() == "MEMORY.MD":
                report.skipped += 1
                continue
            try:
                action = import_file(conn, path, default_origin=default_origin)
                if action == "inserted":
                    report.inserted += 1
                else:
                    report.updated += 1
            except Exception as exc:  # noqa: BLE001 - report and continue
                report.failed.append((path.name, repr(exc)))
    return report
