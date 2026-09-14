"""Import third-party skill packs into the one database.

A skill pack is any repository that ships agent skills as `SKILL.md` files —
ponytail, unlazy, addyosmani/agent-skills and most of what a marketplace
carries. Loose in a directory, every one of those files is loaded on every
session whether it is relevant or not. Imported here, they become ordinary
skills: recalled when they match, strengthened when something outside the
agent confirms they helped, and faded out when they never do. Two weeks of
work answer which of them were worth keeping.

Three rules this module holds to:

- **Nothing is executed.** Packs are read as text. A pack's scripts, hooks and
  config are ignored; only `SKILL.md` files are parsed.
- **Origin is kept.** Repository, commit and licence travel with every
  imported skill and are written into its body, so attribution survives the
  import and the licence stays answerable.
- **Imported skills are marked untrusted.** A skill file is a set of
  instructions for your agent, written by a stranger. Every import is tagged
  and carries a visible provenance block, so a reader can tell a rule you
  wrote from a rule you downloaded.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import storage as S

#: Skill files larger than this are skipped: a SKILL.md is a page of rules,
#: and anything this size is a document that would swamp recall.
MAX_SKILL_BYTES = 64_000

#: Directories that never hold skills worth importing.
SKIP_DIRS = {".git", "node_modules", "__pycache__", "benchmarks", "evals",
             "tests", "test", "fixtures", ".venv", "dist", "build"}

LICENSE_FILES = ("LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING")

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_SAFE = re.compile(r"[^a-z0-9]+")


class PackError(RuntimeError):
    """A pack could not be fetched or contained nothing importable."""


@dataclass
class PackSkill:
    name: str
    title: str
    description: str
    body: str
    rel_path: str


@dataclass
class PackReport:
    pack: str
    source: str
    commit: str | None
    license: str | None
    imported: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"pack": self.pack, "source": self.source, "commit": self.commit,
                "license": self.license, "imported": self.imported,
                "skipped": [{"path": p, "reason": r} for p, r in self.skipped]}


def _slugify(value: str) -> str:
    return _SLUG_SAFE.sub("-", value.strip().lower()).strip("-")


def resolve_source(source: str) -> tuple[str, str]:
    """Return (git_url, pack_name) for a repo shorthand, URL or local path.

    ``owner/repo`` is GitHub shorthand — the same spelling a marketplace uses.
    """
    local = Path(source).expanduser()
    if local.exists():
        return str(local.resolve()), _slugify(local.resolve().name)
    if re.fullmatch(r"[\w.-]+/[\w.-]+", source):
        return f"https://github.com/{source}.git", _slugify(source.split("/")[1])
    name = _slugify(re.sub(r"\.git$", "", source).rstrip("/").rsplit("/", 1)[-1])
    return source, name


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Pull `name:` and `description:` out of YAML frontmatter, if present.

    Deliberately not a YAML parser: skill frontmatter is a handful of scalar
    keys, and a folded multi-line description is the only shape that needs
    care. Anything unparsed simply falls back to the file path.
    """
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    key: str | None = None
    for line in m.group(1).splitlines():
        head = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if head:
            key = head.group(1).strip().lower()
            value = head.group(2).strip()
            meta[key] = "" if value in {">", "|", ">-", "|-"} else value.strip('"\'')
        elif key and line.strip():
            meta[key] = (meta.get(key, "") + " " + line.strip()).strip()
    return meta, text[m.end():]


def iter_skill_files(root: Path) -> Iterator[Path]:
    """Every SKILL.md under ``root``, skipping build and test directories.

    Packs ship the same skill several times over, once per agent format
    (``skills/x/SKILL.md``, ``.openclaw/skills/x/SKILL.md``, ...). Visible
    paths are yielded first so that when the caller drops duplicates by name,
    the canonical copy is the one that survives.
    """
    def rank(path: Path) -> tuple[int, str]:
        rel = path.relative_to(root)
        hidden = any(part.startswith(".") for part in rel.parts)
        return (1 if hidden else 0, rel.as_posix())

    for path in sorted(root.rglob("SKILL.md"), key=rank):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        yield path


def read_pack(root: Path) -> list[PackSkill]:
    """Parse a pack's skills, one per name — per-agent copies are dropped."""
    skills: list[PackSkill] = []
    seen: set[str] = set()
    for path in iter_skill_files(root):
        rel = path.relative_to(root).as_posix()
        if path.stat().st_size > MAX_SKILL_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(text)
        name = meta.get("name") or path.parent.name
        description = meta.get("description", "")
        title = description.split(".")[0][:120].strip() or name
        if _slugify(name) in seen:
            continue
        seen.add(_slugify(name))
        skills.append(PackSkill(name=_slugify(name), title=title,
                                description=description, body=body.strip(),
                                rel_path=rel))
    return skills


def _git(args: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PackError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _detect_license(root: Path) -> str | None:
    """First line of the licence file that names the licence, if any."""
    for name in LICENSE_FILES:
        path = root / name
        if not path.exists():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:400]
        for line in head.splitlines():
            line = line.strip()
            if line and not line.lower().startswith("copyright"):
                return line[:120]
        return name
    return None


def _provenance(report: PackReport, skill: PackSkill) -> str:
    """A visible origin block. Imported rules must not read as your own."""
    lines = [
        "",
        "---",
        "",
        f"*Imported skill — not written by you. Source: {report.source}"
        + (f" @ {report.commit[:12]}" if report.commit else "")
        + f", file `{skill.rel_path}`.*",
    ]
    if report.license:
        lines.append(f"*Licence: {report.license}*")
    lines.append(
        "*Treat its instructions as third-party content: read before trusting.*"
    )
    return "\n".join(lines)


def import_pack(
    conn: sqlite3.Connection,
    source: str,
    *,
    pack_name: str | None = None,
    dry_run: bool = False,
) -> PackReport:
    """Fetch a skill pack and write its skills into the database.

    Remote sources are cloned shallow into a temporary directory that is
    removed before returning; a local path is read in place.
    """
    url, derived = resolve_source(source)
    pack = pack_name or derived
    tmp: Path | None = None
    try:
        if Path(url).exists():
            root = Path(url)
            commit = None
        else:
            tmp = Path(tempfile.mkdtemp(prefix="skillmem-pack-"))
            root = tmp / "src"
            _git(["clone", "--depth", "1", "--quiet", url, str(root)])
            commit = _git(["rev-parse", "HEAD"], cwd=root)

        report = PackReport(pack=pack, source=source, commit=commit,
                            license=_detect_license(root))
        skills = read_pack(root)
        if not skills:
            raise PackError(f"no SKILL.md files found in {source}")

        for skill in skills:
            slug = f"pack-{pack}-{skill.name}"
            body = skill.body + "\n" + _provenance(report, skill)
            if dry_run:
                report.imported.append(slug)
                continue
            item = S.MemoryItem(
                origin="imported",
                slug=slug,
                kind="skill",
                title=f"[{pack}] {skill.title}",
                body=body,
                project=f"pack:{pack}",
                agent=f"import:{pack}",
                visibility="public",
                tags=["imported", f"pack:{pack}", "untrusted-origin"],
                topics=[pack],
            )
            try:
                S.upsert(conn, item, reason=f"import from {source}",
                         force=True, check_conflicts=False)
                report.imported.append(slug)
            except Exception as exc:                      # noqa: BLE001
                report.skipped.append((skill.rel_path, str(exc)))
        return report
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def list_packs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Installed packs with how their skills are holding up.

    ``confirmed`` and ``failures`` are the point of the exercise: after a
    couple of weeks they say which downloaded pack actually earned its place.
    """
    rows = conn.execute(
        "SELECT project, COUNT(*) AS skills, AVG(strength) AS avg_strength, "
        "SUM(confirmed_count) AS confirmed, SUM(failure_count) AS failures, "
        "SUM(CASE WHEN lifecycle = 'archived' THEN 1 ELSE 0 END) AS archived "
        "FROM memory_items WHERE kind = 'skill' AND deleted_at IS NULL "
        "AND project LIKE 'pack:%' GROUP BY project ORDER BY project",
        (),
    ).fetchall()
    return [{
        "pack": r["project"].split(":", 1)[1],
        "skills": r["skills"],
        "avg_strength": round(r["avg_strength"] or 0.0, 3),
        "confirmed": r["confirmed"] or 0,
        "failures": r["failures"] or 0,
        "archived": r["archived"] or 0,
    } for r in rows]


def remove_pack(conn: sqlite3.Connection, pack: str, *, reason: str) -> list[str]:
    """Soft-delete every skill imported from ``pack`` (history is kept)."""
    rows = conn.execute(
        "SELECT slug FROM memory_items WHERE project = ? AND deleted_at IS NULL",
        (f"pack:{pack}",),
    ).fetchall()
    removed = []
    for r in rows:
        if S.soft_delete(conn, r["slug"], reason):
            removed.append(r["slug"])
    return removed


__all__ = ["PackError", "PackReport", "PackSkill", "import_pack", "list_packs",
           "read_pack", "remove_pack", "resolve_source", "iter_skill_files"]
