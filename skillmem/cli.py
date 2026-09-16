"""skillmem command line interface.

Minimal set: init / migrate / search / cat / ls / write / rm / doctor.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from . import storage as S
from .export import export_all
from .migrate import DEFAULT_SOURCE_DIR, discover_claude_memory_dirs, import_dir
from .vault import import_vault


def _owner_trust() -> tuple[int | None, str | None]:
    """A person at a terminal approving their own write — the one honest signal.

    An agent can call the CLI through Bash as easily as a human can type it, so
    the TTY is what separates them: an agent's subprocess has none. Written by an
    agent, a memory arrives as data until the owner approves it.
    """
    import time as _t
    if sys.stdin.isatty() or sys.stdout.isatty():
        return int(_t.time()), "cli-tty"
    return None, None


def _conn(db_path: Path | None):
    conn = S.connect(db_path)
    S.init_schema(conn)
    return conn


from . import __version__


@click.group(help="skillmem CLI (skillmem)")
@click.version_option(__version__, prog_name="skillmem")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=f"Path to SQLite DB (default: {S.default_db_path()})",
)
@click.pass_context
def main(ctx: click.Context, db_path: Path | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path
    if db_path is not None and str(db_path) != ":memory:":
        db_path = db_path.expanduser().resolve()   # a relative --db must not land in a plist
        ctx.obj["db_path"] = db_path
        # so scheduled jobs (schedule._job_env) and anything reading
        # default_db_path() in this process see the same database
        os.environ["SKILLMEM_DB"] = str(db_path)


# NOTE: a second `init` command is defined further down (the installer that
# also wires up Claude Code). Click registers by name, so the later definition
# silently replaced this one — it was dead code and has been removed. Use
# `skillmem doctor` for the "create DB + print stats" behaviour it had.


@main.command()
@click.option(
    "--source",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=DEFAULT_SOURCE_DIR,
    help="Source directory with .md memories.",
)
@click.pass_context
def migrate(ctx: click.Context, source: Path) -> None:
    """Import .md memories from Claude Code auto-memory."""
    conn = _conn(ctx.obj["db_path"])
    report = import_dir(conn, source)
    click.echo(
        f"inserted={report.inserted} updated={report.updated} "
        f"skipped={report.skipped} failed={len(report.failed)}"
    )
    for name, err in report.failed:
        click.echo(f"  ! {name}: {err}", err=True)


@main.command()
@click.argument("query")
@click.option("--kind", default=None, help="Filter by kind (feedback/project/...)")
@click.option("--project", default=None)
@click.option("--limit", default=10, show_default=True)
@click.option("--notes/--no-notes", "with_notes", default=False,
              help="Include session recaps. Hidden by default: they outnumber "
                   "everything else and crowd skills out of the top results.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (text for humans, json for hooks).",
)
@click.pass_context
def search(
    ctx: click.Context, query: str, kind: str | None, project: str | None, limit: int,
    with_notes: bool, fmt: str,
) -> None:
    """Full-text search via FTS5 BM25."""
    conn = _conn(ctx.obj["db_path"])
    excluded = kind is None and not with_notes
    # Session recaps accumulate one per session and can be 90% of the words in
    # the database. They are excluded inside the ranking query, not afterwards:
    # the candidate pool is capped, so a wall of recaps would fill it and hide
    # every skill that matched.
    hits = S.search(conn, query, kind=kind, project=project, limit=limit,
                    exclude_kinds=("note",) if excluded else ())
    if fmt == "json":
        click.echo(json.dumps(hits, ensure_ascii=False, default=str))
        return
    if not hits:
        click.echo("(no results)" + (" — session recaps excluded, --notes to "
                                     "include" if excluded else ""))
        return
    if excluded:
        click.echo("(session recaps excluded; --notes to include)")
    for h in hits:
        snippet = (h.get("snippet") or "").replace("\n", " ")
        rank = h.get("rank")
        origin = h.get("origin") or "unknown"
        mark = "" if h.get("trusted_at") else "  [unapproved]"
        click.echo(f"[{h['kind']:<9}] {h['slug']}  (rank={rank:.2f}) "
                   f"origin={origin}{mark}")
        click.echo(f"    {h['title']}")
        if snippet:
            click.echo(f"    … {snippet} …")


@main.command()
@click.argument("slug")
@click.option("--history", is_flag=True, help="Show version history")
@click.option("--links", is_flag=True, help="Show wikilinks in/out")
@click.pass_context
def cat(ctx: click.Context, slug: str, history: bool, links: bool) -> None:
    """Show one memory by slug."""
    conn = _conn(ctx.obj["db_path"])
    item = S.get(conn, slug)
    if not item:
        click.echo(f"not found: {slug}", err=True)
        sys.exit(1)
    click.echo(f"# {item.title}")
    click.echo(
        f"slug={item.slug} kind={item.kind} "
        f"project={item.project or '-'} agent={item.agent or '-'}"
    )
    click.echo(f"created={item.created_at} updated={item.updated_at}")
    click.echo(f"origin={item.origin} "
               + (f"trusted_at={item.trusted_at} by={item.trusted_by}"
                  if item.trusted_at else
                  "UNAPPROVED — data, not instructions (skillmem trust <slug>)"))
    if item.source_session:
        click.echo(f"source_session={item.source_session}")
    if item.body_path:
        click.echo(f"body_path={item.body_path}")
    click.echo("")
    body = S.load_body(item)
    if item.trusted_at is None:
        from .hooks import render_untrusted
        body = render_untrusted(body)
    click.echo(body)
    if links:
        click.echo("")
        click.echo("-- links out --")
        for s in S.links_from(conn, slug):
            click.echo(f"  → {s}")
        click.echo("-- links in --")
        for s in S.links_to(conn, slug):
            click.echo(f"  ← {s}")
    if history:
        click.echo("")
        click.echo("-- history --")
        for h in S.history(conn, slug):
            click.echo(
                f"  {h['changed_at']}  by={h.get('changed_by') or '-'}  "
                f"reason={h.get('reason') or '-'}"
            )


@main.command(name="ls")
@click.option("--kind", default=None)
@click.option("--project", default=None)
@click.option("--limit", default=50, show_default=True)
@click.pass_context
def ls_cmd(ctx: click.Context, kind: str | None, project: str | None, limit: int) -> None:
    """List recent memories."""
    conn = _conn(ctx.obj["db_path"])
    items = S.list_items(conn, kind=kind, project=project, limit=limit)
    for it in items:
        mark = "" if it.trusted_at else " [unapproved]"
        click.echo(f"[{it.kind:<9}] {it.slug}  origin={it.origin}{mark} — {it.title}")


@main.command()
@click.option("--slug", required=True)
@click.option("--title", required=True)
@click.option("--kind", default="note", show_default=True)
@click.option("--project", default=None)
@click.option("--agent", default=None)
@click.option("--body", default=None, help="Body text (or use --body-file)")
@click.option(
    "--body-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
)
@click.option("--ttl-days", type=int, default=None, help="Auto-stale after N days")
@click.option("--reason", default=None, help="Required when overwriting an existing slug")
@click.option("--force", is_flag=True)
@click.option("--check-conflicts/--no-check-conflicts", default=True,
              help="Refuse near-duplicates (word overlap > 0.7)")
@click.pass_context
def write(
    ctx: click.Context,
    slug: str,
    title: str,
    kind: str,
    project: str | None,
    agent: str | None,
    body: str | None,
    body_file: Path | None,
    ttl_days: int | None,
    reason: str | None,
    force: bool,
    check_conflicts: bool,
) -> None:
    """Insert or update a memory."""
    conn = _conn(ctx.obj["db_path"])
    if body_file:
        body_text = body_file.read_text(encoding="utf-8")
    elif body is not None:
        body_text = body
    else:
        body_text = sys.stdin.read()

    trusted_at, trusted_by = _owner_trust()
    item = S.MemoryItem(
        slug=slug, kind=kind, title=title, body=body_text,
        project=project, agent=agent, ttl_days=ttl_days,
        # Provenance follows the channel: a terminal means a person typed it;
        # no terminal means an agent ran the CLI, and "owner" would be a lie.
        origin="owner" if trusted_at else "agent",
        trusted_at=trusted_at, trusted_by=trusted_by,
    )
    try:
        result = S.upsert(
            conn, item, reason=reason, force=force,
            check_conflicts=check_conflicts,
            links=S.extract_wikilinks(body_text),
        )
    except S.MemoryConflict as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    click.echo(f"OK: {result.slug} (id={result.id})")


@main.command()
@click.argument("slug")
@click.option("--reason", required=True)
@click.pass_context
def rm(ctx: click.Context, slug: str, reason: str) -> None:
    """Soft-delete a memory (kept in memory_history)."""
    conn = _conn(ctx.obj["db_path"])
    if S.soft_delete(conn, slug, reason):
        click.echo(f"deleted: {slug}")
    else:
        click.echo(f"not found: {slug}", err=True)
        sys.exit(1)


@main.command()
@click.option("--types", default="user,feedback",
              help="Comma-separated kinds to inject (default: user,feedback)")
@click.option("--budget", "budget_tokens", default=2000, show_default=True,
              type=int, help="Approximate token budget")
@click.option("--format", "fmt",
              type=click.Choice(["text", "md", "json"]), default="md",
              show_default=True)
@click.option("--per-kind", default=30, show_default=True, type=int)
@click.pass_context
def inject(
    ctx: click.Context,
    types: str,
    budget_tokens: int,
    fmt: str,
    per_kind: int,
) -> None:
    """Compact title-only briefing for SessionStart hook."""
    conn = _conn(ctx.obj["db_path"])
    kinds = [t.strip() for t in types.split(",") if t.strip()]
    brief = S.briefing(
        conn, kinds=kinds, budget_tokens=budget_tokens, per_kind_limit=per_kind,
    )
    if fmt == "json":
        click.echo(json.dumps(brief, ensure_ascii=False, indent=2))
        return
    lines: list[str] = []
    if fmt == "md":
        lines.append("# skillmem briefing\n")
    for sec in brief["sections"]:
        title = sec["kind"].upper()
        lines.append(f"## {title}" if fmt == "md" else title)
        for it in sec["items"]:
            lines.append(f"- [{it['slug']}] {it['title']}")
        lines.append("")
    if brief["omitted"]:
        suffix = f"({brief['omitted']} omitted, budget={brief['budget_tokens']} tk)"
        lines.append(f"_… {suffix}_" if fmt == "md" else suffix)
    if brief.get("unapproved"):
        # Said as a count, never as content: the briefing is title-only, and an
        # unapproved title belongs behind a frame, which this format has no room
        # for. `skillmem search --notes` and `skillmem trust <slug>` are the way in.
        note = (f"{brief['unapproved']} unapproved memories are NOT shown here "
                "(data, not rules — approve with `skillmem trust <slug>`)")
        lines.append(f"_{note}_" if fmt == "md" else note)
    click.echo("\n".join(lines))


@main.command("trust")
@click.argument("slug")
@click.option("--untrust", is_flag=True, help="Withdraw approval instead.")
@click.pass_context
def trust_cmd(ctx: click.Context, slug: str, untrust: bool) -> None:
    """Approve a memory so hooks may present it as a rule (or withdraw approval).

    Only the owner grants trust. An agent can be talked into saving a rule by the
    document it was reading, so what an agent wrote arrives unapproved — as data.
    Editing an approved memory's text drops the approval with it.
    """
    # The same signal write/learn use: no terminal, no owner. An agent that is
    # talked into `skillmem trust <slug>` from Bash must get a refusal, not an
    # approval — otherwise one command undoes the whole trust boundary. This is
    # accident protection, not a wall: `init --claude-code` also installs a
    # permission deny rule for the command, and README says so.
    if _owner_trust()[0] is None:
        # both directions: an injected `--untrust` would strip a real rule
        raise click.ClickException(
            "refusing: `trust` needs a person at a terminal (no TTY). "
            "Run it yourself, not through an agent."
        )
    conn = _conn(ctx.obj["db_path"])
    item = S.set_trust(conn, slug, trusted=not untrust)
    conn.commit()
    if item is None:
        raise click.ClickException(f"no memory with slug '{slug}'")
    state = ("untrusted" if untrust else
             f"trusted at {item.trusted_at} by {item.trusted_by}")
    click.echo(f"{slug}: origin={item.origin}, {state}")


@main.command("recap")
@click.argument("transcript", required=False,
                type=click.Path(dir_okay=False, path_type=Path))
@click.option("--force/--no-force", default=True, show_default=True,
              help="Ignore the rate limit (that is the point of asking by hand).")
def recap_cmd(transcript: Path | None, force: bool) -> None:
    """Write a session recap now — by default for this project's newest transcript.

    The Stop hook is rate-limited, so the closing minutes of a session may not be
    in memory yet. This is how you save them without waiting.
    """
    from .hooks import newest_transcript_for_cwd, run_recap
    path = transcript or newest_transcript_for_cwd()
    if path is None or not path.is_file():
        raise click.ClickException(
            "no transcript found for this directory — pass one: "
            "skillmem recap ~/.claude/projects/<project>/<session>.jsonl")
    data = {
        "session_id": path.stem,
        "transcript_path": str(path),
        # Not SessionEnd: this run is not inside that event's 60s budget.
        "hook_event_name": "Manual",
        "force": force,
    }
    run_recap(data)
    click.echo(f"recap run for {path.name} (see `skillmem hooks-status`)")


@main.command("reindex-lexical")
@click.pass_context
def reindex_lexical(ctx: click.Context) -> None:
    """Rebuild the lexical (stemmed) index now — minutes on a large database.

    Needed once after 0.10.3: the index used to drop two-character tokens, so
    `db`, `py`, `js`, `ci` were missing from every stored memory. The nightly
    decay job does this on its own; this is the impatient path.
    """
    conn = _conn(ctx.obj["db_path"])
    n = S.restem_all(conn)
    conn.commit()
    click.echo(f"Rebuilt the lexical index for {n} memories.")


@main.command("hooks-status")
@click.option("--lines", default=4000, show_default=True,
              help="How much of the tail of the hook log to read.")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def hooks_status(lines: int, fmt: str) -> None:
    """What the hooks have actually been doing: last run, skips, failures.

    Every hook swallows its own errors so it can never break a session, which
    also means a hook that silently stopped working looks exactly like one with
    nothing to do. This is where you see the difference.
    """
    from .hooks import _hook_log_path, _state_dir
    log = _hook_log_path()
    rows: list[list[str]] = []
    if log.is_file():
        with log.open(encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-lines:]
        rows = [ln.rstrip("\n").split("\t") for ln in tail if "\t" in ln]
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        if len(r) < 2:
            continue
        name = r[1]
        rest = " ".join(r[3:]) if len(r) > 3 else ""
        e = per.setdefault(name, {"runs": 0, "last": "", "last_detail": "",
                                  "skipped": 0, "failed": 0})
        e["runs"] += 1
        e["last"], e["last_detail"] = r[0], rest[:120]
        if rest.startswith("skip") or ":busy" in rest or "debounce" in rest:
            e["skipped"] += 1
        if ("empty/failed" in rest or rest.startswith("error")
                or "timeout" in rest or "failed" in rest):
            e["failed"] += 1
    report = {
        "log": str(log),
        "log_exists": log.is_file(),
        "state_dir": str(_state_dir()),
        "recap_stamps": len(list((_state_dir() / "recap-stamps").glob("*.stamp")))
        if (_state_dir() / "recap-stamps").is_dir() else 0,
        "ledgers": len(list((_state_dir() / "injected").glob("*.txt")))
        if (_state_dir() / "injected").is_dir() else 0,
        "hooks": per,
    }
    if fmt == "json":
        click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return
    click.echo(f"log: {report['log']}" + ("" if report["log_exists"] else "  (MISSING)"))
    click.echo(f"state: {report['state_dir']}  stamps={report['recap_stamps']} "
               f"ledgers={report['ledgers']}")
    if not per:
        click.echo("no hook activity in the log tail — hooks may not be wired "
                   "(check `skillmem init` and ~/.claude/settings.json)")
        return
    for name, e in sorted(per.items()):
        click.echo(f"{name:<16} runs={e['runs']:<5} skipped={e['skipped']:<5} "
                   f"failed={e['failed']:<5} last={e['last']}")
        if e["last_detail"]:
            click.echo(f"{'':<16} └ {e['last_detail']}")


@main.command("export-all")
@click.argument(
    "destination",
    type=click.Path(file_okay=False, path_type=Path),
)
@click.pass_context
def export_all_cmd(ctx: click.Context, destination: Path) -> None:
    """Dump every memory back to .md with frontmatter."""
    conn = _conn(ctx.obj["db_path"])
    n = export_all(conn, destination)
    click.echo(f"OK: exported {n} memories to {destination}")


@main.command("import-vault")
@click.argument(
    "path",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
)
@click.option("--project", default=None, help="Override project tag for all imported docs")
@click.option("--kind", default="document", show_default=True)
@click.option("--skip-frontmatter-memories", is_flag=True,
              help="Skip files that already look like Claude Code auto-memories")
@click.pass_context
def import_vault_cmd(
    ctx: click.Context,
    path: Path,
    project: str | None,
    kind: str,
    skip_frontmatter_memories: bool,
) -> None:
    """Import an Obsidian vault (recursive)."""
    conn = _conn(ctx.obj["db_path"])
    report = import_vault(
        conn, path,
        kind=kind,
        project_override=project,
        skip_auto_memories=skip_frontmatter_memories,
    )
    click.echo(
        f"inserted={report.inserted} updated={report.updated} "
        f"skipped={report.skipped} failed={len(report.failed)}"
    )
    for name, err in report.failed[:5]:
        click.echo(f"  ! {name}: {err}", err=True)
    if len(report.failed) > 5:
        click.echo(f"  ... and {len(report.failed) - 5} more failures", err=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write, through a symlink, mode kept — see _atomic_write_text."""
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _patch_claude_json(
    claude_json: Path,
    mcp_binary: Path,
    *,
    db_env: str | None = None,
) -> dict[str, Any]:
    """Add a ``mcpServers.skillmem`` entry to ~/.claude.json.

    Reads → backups → writes atomically. If the existing JSON is corrupt,
    logs a clear warning and refuses to overwrite (user reviews the .bak).
    """
    import time as _time
    data: dict[str, Any] = {}
    backup: Path | None = None
    if claude_json.exists():
        raw = claude_json.read_text(encoding="utf-8")
        backup = claude_json.with_suffix(f".json.bak.{int(_time.time())}")
        backup.write_text(raw, encoding="utf-8")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            click.echo(
                f"warn: {claude_json} contains invalid JSON ({exc}); refusing to "
                f"overwrite. Inspect backup at {backup} and re-run init after fixing.",
                err=True,
            )
            return {"changed": False, "reason": "existing JSON is invalid",
                    "backup": str(backup)}

    servers = data.setdefault("mcpServers", {})
    if "skillmem" in servers:
        # an existing entry is kept — except the database it points at, which
        # must follow --db: an upgrader who re-runs init --db X used to keep
        # the old (or no) SKILLMEM_DB forever
        entry = servers["skillmem"]
        if not isinstance(entry, dict):
            return {"changed": False, "reason": "mcpServers.skillmem is not an object; fix by hand"}
        env = entry.get("env")
        if not isinstance(env, dict):
            env = entry["env"] = {}   # a hand-edited null/list/string env: replace, not crash
        if db_env and env.get("SKILLMEM_DB") != db_env:
            env["SKILLMEM_DB"] = db_env
            _atomic_write_json(claude_json, data)
            return {"changed": True, "added": f"mcpServers.skillmem.env.SKILLMEM_DB={db_env}",
                    "backup": str(backup) if backup else None}
        return {"changed": False, "reason": "skillmem MCP already configured",
                "backup": str(backup) if backup else None}

    entry: dict[str, Any] = {"command": str(mcp_binary), "args": []}
    if db_env:
        entry["env"] = {"SKILLMEM_DB": db_env}
    servers["skillmem"] = entry
    _atomic_write_json(claude_json, data)
    return {"changed": True, "added": "mcpServers.skillmem",
            "backup": str(backup) if backup else None}


#: Editors that read the Claude-shaped ``{"mcpServers": {...}}`` map. The path
#: and the ``SKILLMEM_AGENT`` stamp are all that differ between them.
MCP_JSON_AGENTS: dict[str, tuple[tuple[str, ...], str]] = {
    "cursor": ((".cursor", "mcp.json"), "Cursor"),
    "windsurf": ((".codeium", "windsurf", "mcp_config.json"), "Windsurf"),
    "gemini": ((".gemini", "settings.json"), "Gemini CLI"),
}

#: opencode keeps its servers under ``mcp`` in the global config instead.
OPENCODE_CONFIG = (".config", "opencode", "opencode.json")


def _agent_config_path(parts: tuple[str, ...]) -> Path:
    return Path.home().joinpath(*parts)


def _read_json_config(path: Path) -> tuple[dict[str, Any], Path | None, str | None]:
    """Read a JSON config, backing it up first. Returns (data, backup, error).

    A non-None error means the file is there but unparseable — callers refuse to
    touch it and point the user at the backup, same as ``_patch_claude_json``.
    """
    import time as _time
    if not path.exists():
        return {}, None, None
    raw = path.read_text(encoding="utf-8")
    backup = path.with_suffix(f"{path.suffix}.bak.{int(_time.time())}")
    backup.write_text(raw, encoding="utf-8")
    try:
        return (json.loads(raw) if raw.strip() else {}), backup, None
    except json.JSONDecodeError as exc:
        return {}, backup, str(exc)



def _update_env_in_place(entry: Any, env_key: str, db_env: str | None) -> str | None:
    """An existing MCP entry keeps everything except the database it points
    at, which follows an explicit --db. Returns a description when changed."""
    if not db_env or not isinstance(entry, dict):
        return None
    env = entry.get(env_key)
    if not isinstance(env, dict):
        env = entry[env_key] = {}
    if env.get("SKILLMEM_DB") == db_env:
        return None
    env["SKILLMEM_DB"] = db_env
    return f"{env_key}.SKILLMEM_DB={db_env}"


def _patch_mcp_servers_json(
    config_json: Path,
    mcp_binary: Path,
    *,
    agent: str,
    db_env: str | None = None,
) -> dict[str, Any]:
    """Add ``mcpServers.skillmem`` to an editor config in the Claude shape.

    ``SKILLMEM_AGENT`` marks every skill the editor writes, so authorship stays
    answerable in a database shared by several agents.
    """
    data, backup, err = _read_json_config(config_json)
    if err:
        click.echo(
            f"warn: {config_json} contains invalid JSON ({err}); refusing to "
            f"overwrite. Inspect backup at {backup} and re-run init after fixing.",
            err=True,
        )
        return {"changed": False, "reason": "existing JSON is invalid",
                "backup": str(backup)}

    servers = data.setdefault("mcpServers", {})
    if "skillmem" in servers:
        r = _update_env_in_place(servers["skillmem"], "env", db_env)
        if r:
            _atomic_write_json(config_json, data)
            return {"changed": True, "added": r, "agent": agent, "path": str(config_json),
                    "backup": str(backup) if backup else None}
        return {"changed": False, "reason": "skillmem MCP already configured",
                "backup": str(backup) if backup else None}

    env: dict[str, str] = {"SKILLMEM_AGENT": agent}
    if db_env:
        env["SKILLMEM_DB"] = db_env
    servers["skillmem"] = {"command": str(mcp_binary), "args": [], "env": env}
    _atomic_write_json(config_json, data)
    return {"changed": True, "added": "mcpServers.skillmem", "agent": agent,
            "path": str(config_json),
            "backup": str(backup) if backup else None}


def _unpatch_mcp_servers_json(config_json: Path) -> dict[str, Any]:
    """Remove the ``mcpServers.skillmem`` entry added by init."""
    if not config_json.exists():
        return {"changed": False, "reason": f"no {config_json.name}"}
    data, backup, err = _read_json_config(config_json)
    if err:
        return {"changed": False, "reason": f"could not parse {config_json}"}
    servers = data.get("mcpServers") or {}
    if "skillmem" not in servers:
        return {"changed": False, "reason": "skillmem MCP not configured"}
    del servers["skillmem"]
    if not servers:
        data.pop("mcpServers", None)
    _atomic_write_json(config_json, data)
    return {"changed": True, "removed": "mcpServers.skillmem",
            "path": str(config_json), "backup": str(backup) if backup else None}


def _patch_opencode_json(
    config_json: Path,
    mcp_binary: Path,
    *,
    db_env: str | None = None,
    agent: str = "opencode",
) -> dict[str, Any]:
    """Add an ``mcp.skillmem`` local server to opencode's global config.

    opencode has its own shape: servers live under ``mcp``, the command is an
    argv array, and environment variables go in ``environment``.
    """
    data, backup, err = _read_json_config(config_json)
    if err:
        click.echo(
            f"warn: {config_json} contains invalid JSON ({err}); refusing to "
            f"overwrite. Inspect backup at {backup} and re-run init after fixing.",
            err=True,
        )
        return {"changed": False, "reason": "existing JSON is invalid",
                "backup": str(backup)}

    servers = data.setdefault("mcp", {})
    if "skillmem" in servers:
        r = _update_env_in_place(servers["skillmem"], "environment", db_env)
        if r:
            _atomic_write_json(config_json, data)
            return {"changed": True, "added": r, "agent": agent, "path": str(config_json),
                    "backup": str(backup) if backup else None}
        return {"changed": False, "reason": "skillmem MCP already configured",
                "backup": str(backup) if backup else None}

    env: dict[str, str] = {"SKILLMEM_AGENT": agent}
    if db_env:
        env["SKILLMEM_DB"] = db_env
    servers["skillmem"] = {
        "type": "local",
        "command": [str(mcp_binary)],
        "enabled": True,
        "environment": env,
    }
    _atomic_write_json(config_json, data)
    return {"changed": True, "added": "mcp.skillmem", "agent": agent,
            "path": str(config_json),
            "backup": str(backup) if backup else None}


def _unpatch_opencode_json(config_json: Path) -> dict[str, Any]:
    """Remove the ``mcp.skillmem`` server added by init --opencode."""
    if not config_json.exists():
        return {"changed": False, "reason": "no opencode.json"}
    data, backup, err = _read_json_config(config_json)
    if err:
        return {"changed": False, "reason": f"could not parse {config_json}"}
    servers = data.get("mcp") or {}
    if "skillmem" not in servers:
        return {"changed": False, "reason": "skillmem MCP not configured"}
    del servers["skillmem"]
    if not servers:
        data.pop("mcp", None)
    _atomic_write_json(config_json, data)
    return {"changed": True, "removed": "mcp.skillmem",
            "path": str(config_json), "backup": str(backup) if backup else None}


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic write for plain text: tempfile next to the TARGET + os.replace.

    A dotfile-managed config is often a symlink; replacing the link with a
    regular file silently forked it from the repo. Write through to the real
    file, keep its mode, and keep its bytes as given (no newline translation).
    """
    import os as _os, stat as _stat, tempfile as _tempfile
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    try:
        mode = _stat.S_IMODE(target.stat().st_mode)
    except OSError:
        pass
    fd, tmp = _tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if mode is not None:
            _os.chmod(tmp, mode)
        _os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _toml_str(value: str) -> str:
    """TOML basic string. Escapes backslashes first — Windows paths break otherwise."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'




def _patch_codex_config(
    config_toml: Path,
    mcp_binary: Path,
    *,
    db_env: str | None = None,
    agent: str = "codex",
) -> dict[str, Any]:
    """Add an ``[mcp_servers.skillmem]`` table to ~/.codex/config.toml.

    Appends rather than rewrites: the file is hand-edited by users and full
    round-tripping would drop their comments. New tables at the end of a TOML
    document are always valid, and the result is parsed before it is written —
    if appending would corrupt the file we refuse and keep the backup.

    ``SKILLMEM_AGENT`` marks every skill Codex writes, so authorship stays
    visible in a database shared with Claude Code.
    """
    import time as _time
    import tomllib

    raw = ""
    backup: Path | None = None
    if config_toml.exists():
        raw = config_toml.read_bytes().decode("utf-8")   # keep CRLF as is
        backup = config_toml.with_suffix(f".toml.bak.{int(_time.time())}")
        backup.write_bytes(raw.encode("utf-8"))           # byte-exact, no newline translation
        try:
            parsed = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            click.echo(
                f"warn: {config_toml} contains invalid TOML ({exc}); refusing to "
                f"touch it. Inspect backup at {backup} and re-run init after fixing.",
                err=True,
            )
            return {"changed": False, "reason": "existing TOML is invalid",
                    "backup": str(backup)}
        servers = parsed.get("mcp_servers")
        if servers is not None and not isinstance(servers, dict):
            return {"changed": False, "reason": "mcp_servers is not a table; edit it by hand",
                    "backup": str(backup)}
        if "skillmem" in (servers or {}):
            # An existing table is left exactly as it is — including the
            # database it points at. Editing a hand-written TOML in place was
            # tried and withdrawn: four review rounds found a new edge each
            # (multi-line strings, comment boundaries, CRLF), and a config
            # file is not worth that. Moving Codex to another database is
            # two explicit commands, or one line by hand.
            current = None
            entry = servers["skillmem"]
            if isinstance(entry, dict) and isinstance(entry.get("env"), dict):
                current = entry["env"].get("SKILLMEM_DB")
            if db_env and current != db_env:
                return {"changed": False, "backup": str(backup),
                        "reason": f"skillmem MCP already configured for "
                                  f"{current or 'the default database'}; to point Codex at "
                                  f"{db_env}, set SKILLMEM_DB = {_toml_str(db_env)} under "
                                  f"[mcp_servers.skillmem.env] in {config_toml} by hand"}
            return {"changed": False, "reason": "skillmem MCP already configured",
                    "backup": str(backup)}


    env: dict[str, str] = {"SKILLMEM_AGENT": agent}
    if db_env:
        env["SKILLMEM_DB"] = db_env

    lines = ["", "[mcp_servers.skillmem]",
             f"command = {_toml_str(str(mcp_binary))}",
             "args = []",
             "startup_timeout_sec = 30",
             "", "[mcp_servers.skillmem.env]"]
    lines += [f"{k} = {_toml_str(v)}" for k, v in env.items()]

    if raw and not raw.endswith("\n"):
        raw += "\n"
    new_raw = raw + "\n".join(lines) + "\n"

    try:
        tomllib.loads(new_raw)
    except tomllib.TOMLDecodeError as exc:
        return {"changed": False, "reason": f"appending would break the file: {exc}",
                "backup": str(backup) if backup else None}

    _atomic_write_text(config_toml, new_raw)
    return {"changed": True, "added": "mcp_servers.skillmem",
            "agent": agent,
            "backup": str(backup) if backup else None}


def _unpatch_codex_config(config_toml: Path) -> dict[str, Any]:
    """Remove the ``[mcp_servers.skillmem]`` tables added by init --codex.

    Line-based on purpose, symmetric with the append above: drop the skillmem
    tables and their sub-tables, leave every other line (comments included)
    exactly where the user put it.
    """
    import time as _time
    import tomllib

    if not config_toml.exists():
        return {"changed": False, "reason": "no config.toml"}
    raw = config_toml.read_bytes().decode("utf-8")   # byte-exact backup, CRLF kept
    try:
        parsed = tomllib.loads(raw)
    except tomllib.TOMLDecodeError:
        return {"changed": False, "reason": f"could not parse {config_toml}"}
    if not isinstance(parsed.get("mcp_servers"), dict) or "skillmem" not in parsed["mcp_servers"]:
        return {"changed": False, "reason": "skillmem MCP not configured"}

    out: list[str] = []
    dropping = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            header = stripped.strip("[]").strip()
            dropping = (header == "mcp_servers.skillmem"
                        or header.startswith("mcp_servers.skillmem."))
        if not dropping:
            out.append(line)

    while out and not out[-1].strip():
        out.pop()
    new_raw = "\n".join(out) + ("\n" if out else "")
    # Line-level removal from a hand-written TOML: accept it only if the
    # result parses to exactly the old document minus [mcp_servers.skillmem]
    # — a header inside a multi-line string, a commented header, anything
    # else, and we refuse rather than write a broken config.
    expect = json.loads(json.dumps(parsed, default=str))
    expect["mcp_servers"].pop("skillmem", None)
    if not expect["mcp_servers"]:
        expect.pop("mcp_servers")
    try:
        got = json.loads(json.dumps(tomllib.loads(new_raw), default=str))
        if got.get("mcp_servers") == {}:
            got.pop("mcp_servers")   # an explicit, now-empty [mcp_servers] header
    except tomllib.TOMLDecodeError:
        got = None
    if got != expect:
        return {"changed": False, "reason": "could not remove [mcp_servers.skillmem] "
                "cleanly; delete the table by hand", "backup": None}
    backup = config_toml.with_suffix(f".toml.bak.{int(_time.time())}")
    backup.write_bytes(raw.encode("utf-8"))
    _atomic_write_text(config_toml, new_raw)
    return {"changed": True, "removed": "mcp_servers.skillmem",
            "backup": str(backup)}


def _venv_script(name: str) -> Path:
    """Console script next to the interpreter: bin/<name> or Scripts\\<name>.exe."""
    scripts = Path(sys.executable).parent
    return scripts / (f"{name}.exe" if sys.platform == "win32" else name)


def _hook_cmd(binary: Path, args: list[str]) -> str:
    """Hook command string with platform-appropriate quoting.

    POSIX — shlex.quote; Windows — list2cmdline (cmd.exe has no notion of
    shlex single quotes, so a path like C:\\Users\\First Last\\… would break).
    """
    parts = [str(binary), *args]
    if sys.platform == "win32":
        import subprocess as _subprocess
        return _subprocess.list2cmdline(parts)
    import shlex as _shlex
    return " ".join(_shlex.quote(p) for p in parts)


def _patch_settings_hook(
    settings_json: Path,
    binary: Path,
    *,
    event: str,
    args: list[str],
    matcher: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Add a hook into ``~/.claude/settings.json`` if not already present.

    Dedup is by the full command (not the binary path): one event can carry
    several distinct skillmem hooks (verify-gate + auto-recall).
    """
    import time as _time
    data: dict[str, Any] = {}
    backup: Path | None = None
    if settings_json.exists():
        raw = settings_json.read_text(encoding="utf-8")
        backup = settings_json.with_suffix(f".json.bak.{int(_time.time())}")
        backup.write_text(raw, encoding="utf-8")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            click.echo(
                f"warn: {settings_json} contains invalid JSON ({exc}); "
                f"refusing to overwrite. Inspect backup at {backup}.",
                err=True,
            )
            return {"changed": False, "reason": "existing JSON is invalid",
                    "backup": str(backup)}

    hooks = data.setdefault("hooks", {})
    event_hooks = hooks.setdefault(event, [])
    cmd_str = _hook_cmd(binary, args)
    for group in event_hooks:
        for h in group.get("hooks", []) or []:
            if h.get("command", "") == cmd_str:
                return {"changed": False,
                        "reason": f"{event} hook already present: {' '.join(args)}",
                        "backup": str(backup) if backup else None}
    group: dict[str, Any] = {
        "hooks": [{"type": "command", "command": cmd_str, "timeout": timeout}]
    }
    if matcher:
        group["matcher"] = matcher
    event_hooks.append(group)
    _atomic_write_json(settings_json, data)
    return {"changed": True, "added": f"hooks.{event}: {' '.join(args) or 'migrate'}",
            "backup": str(backup) if backup else None}


def _prune_settings_hook(settings_json: Path, *, command_prefix: str) -> dict[str, Any]:
    """Remove hooks whose command ends with ``command_prefix`` (any binary path).

    Upgrades must drop the Stop→migrate hook older inits installed, or the
    wrong-project import keeps running on every turn.
    """
    if not settings_json.exists():
        return {"changed": False, "reason": "no settings.json"}
    try:
        data = json.loads(settings_json.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {"changed": False, "reason": "existing JSON is invalid"}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {"changed": False, "reason": "no hooks"}
    import shlex as _shlex
    want = command_prefix.split()[1:]           # e.g. ["migrate"]

    def _is_ours(cmd: str) -> bool:
        # parse the way the command was written: shlex on POSIX, plain split
        # on Windows (list2cmdline); match the binary by NAME, so a quoted
        # path with a space, skillmem.exe, or any venv location all match —
        # and "my-skillmem migrate" (a foreign tool) does not
        try:
            argv = _shlex.split(cmd, posix=(sys.platform != "win32"))
        except ValueError:
            return False
        if not argv:
            return False
        from pathlib import PureWindowsPath as _WP
        raw = argv[0].strip('"')                        # list2cmdline keeps the quotes
        name = (_WP(raw) if sys.platform == "win32" else Path(raw)).name.lower()
        return name in ("skillmem", "skillmem.exe") and argv[1:] == want

    removed = 0
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        for group in groups:
            hs = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(hs, list):
                continue
            keep = [h for h in hs
                    if not (isinstance(h, dict) and _is_ours(str(h.get("command", ""))))]
            removed += len(hs) - len(keep)
            group["hooks"] = keep
        hooks[event] = [g for g in groups if not (isinstance(g, dict) and g.get("hooks") == [])]
        if not hooks[event]:
            del hooks[event]
    if not removed:
        return {"changed": False, "reason": f"no '{command_prefix}' hook present"}
    import time as _time
    backup = settings_json.with_suffix(f".json.bak.{int(_time.time())}")
    backup.write_text(settings_json.read_text(encoding="utf-8"), encoding="utf-8")
    _atomic_write_json(settings_json, data)
    return {"changed": True, "removed": f"{removed} hook(s) running '{command_prefix}'",
            "backup": str(backup)}


def _patch_settings_deny(settings_json: Path, rule: str) -> dict[str, Any]:
    """Add a permission deny rule to ~/.claude/settings.json (idempotent).

    `skillmem trust` refuses to run without a TTY, but a TTY can be faked
    (`script -q /dev/null skillmem trust x`). The deny rule is what actually
    stops Claude Code from running the command at an injected document's
    request; the TTY check only catches the accidental case.
    """
    data: dict[str, Any] = {}
    if settings_json.exists():
        try:
            data = json.loads(settings_json.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            return {"changed": False, "reason": "existing JSON is invalid"}
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return {"changed": False, "reason": "permissions is not an object"}
    deny = perms.setdefault("deny", [])
    if rule in deny:
        return {"changed": False, "reason": f"deny rule already present: {rule}"}
    deny.append(rule)
    _atomic_write_json(settings_json, data)
    return {"changed": True, "added": f"permissions.deny: {rule}"}


@main.command()
@click.option("--claude-code", is_flag=True,
              help="Configure MCP entry in ~/.claude.json and add hooks")
@click.option("--codex", is_flag=True,
              help="Configure MCP entry in ~/.codex/config.toml (Codex CLI)")
@click.option("--cursor", is_flag=True,
              help="Configure MCP entry in ~/.cursor/mcp.json")
@click.option("--windsurf", is_flag=True,
              help="Configure MCP entry in ~/.codeium/windsurf/mcp_config.json")
@click.option("--gemini", is_flag=True,
              help="Configure MCP entry in ~/.gemini/settings.json (Gemini CLI)")
@click.option("--opencode", is_flag=True,
              help="Configure MCP entry in ~/.config/opencode/opencode.json")
@click.option("--all-agents", is_flag=True,
              help="Every agent above: one database, six agents")
@click.option("--migrate-existing/--skip-migrate", default=True,
              help="Auto-discover and import all ~/.claude/projects/*/memory")
@click.option("--mcp-binary", type=click.Path(path_type=Path), default=None,
              help="Override path to skillmem-mcp (default: auto-detect)")
@click.option("--hooks", "hooks_mode",
              type=click.Choice(["full", "minimal", "none"]), default="full",
              help="full: recall/recap/guard hooks + trust deny rule; "
                   "minimal: deny rule only; none: nothing")
@click.pass_context
def init(
    ctx: click.Context,
    claude_code: bool,
    codex: bool,
    cursor: bool,
    windsurf: bool,
    gemini: bool,
    opencode: bool,
    all_agents: bool,
    migrate_existing: bool,
    mcp_binary: Path | None,
    hooks_mode: str,
) -> None:
    """First-time setup: create DB, migrate auto-memory, wire up your agents."""
    report: dict[str, Any] = {}
    if all_agents:
        claude_code = codex = cursor = windsurf = gemini = opencode = True

    db_path = ctx.obj["db_path"] or S.default_db_path()
    conn = _conn(db_path)
    report["db_path"] = str(db_path)
    report["schema_version"] = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()["value"]

    if migrate_existing:
        migrations: list[dict[str, Any]] = []
        for src in discover_claude_memory_dirs():
            r = import_dir(conn, src)
            migrations.append({
                "source": str(src),
                "inserted": r.inserted, "updated": r.updated,
                "skipped": r.skipped, "failed": len(r.failed),
            })
        report["migrations"] = migrations

    if claude_code:
        if mcp_binary is None:
            mcp_binary = _venv_script("skillmem-mcp")
        if not mcp_binary.exists():
            click.echo(f"warn: {mcp_binary} not found — install package first", err=True)
        claude_json = Path.home() / ".claude.json"
        report["claude_json"] = _patch_claude_json(
            claude_json, mcp_binary,
            db_env=str(ctx.obj["db_path"]) if ctx.obj.get("db_path") else None,
        )

        settings_json = Path.home() / ".claude" / "settings.json"
        skillmem_bin = _venv_script("skillmem")

        def _hook(event: str, args: list[str], **kw: Any) -> dict[str, Any]:
            return _patch_settings_hook(settings_json, skillmem_bin,
                                        event=event, args=args, **kw)

        hook_reports: list[dict[str, Any]] = []
        # No Stop→`skillmem migrate` hook any more: with no --source it
        # imported the alphabetically-first project's memory dir on every
        # turn (not this project's), holding a write lock while doing it, and
        # session-recap already indexes the note it writes. Hand-written
        # memory files are a `skillmem migrate --source <dir>` job.
        if hooks_mode != "none":
            hook_reports.append(_patch_settings_deny(
                settings_json, "Bash(skillmem trust*)"))
            hook_reports.append(_prune_settings_hook(
                settings_json, command_prefix="skillmem migrate"))
        if hooks_mode == "full":
            hook_reports += [
                _hook("SessionStart", ["hook", "mcp-guard"]),
                _hook("SessionStart",
                      ["inject", "--types", "user,feedback", "--budget", "2000"]),
                _hook("SessionStart", ["hook", "session-history"]),
                _hook("UserPromptSubmit", ["hook", "verify-gate"]),
                _hook("UserPromptSubmit", ["hook", "auto-recall"]),
                _hook("PreToolUse", ["hook", "tool-recall"],
                      matcher="Bash|Edit|Write|NotebookEdit"),
                # recap invokes `claude -p` — the timeout must cover the LLM call
                _hook("Stop", ["hook", "session-recap"], timeout=95),
                # Stop fires per turn and is rate-limited; SessionEnd fires once
                # and is not, so the closing turns still reach memory. Claude
                # Code raises the SessionEnd budget to the per-hook timeout but
                # never past 60s, so asking for more would be a lie.
                _hook("SessionEnd", ["hook", "session-recap"], timeout=60),
            ]
        report["hooks"] = hook_reports

    if codex:
        codex_binary = mcp_binary or _venv_script("skillmem-mcp")
        if not codex_binary.exists():
            click.echo(f"warn: {codex_binary} not found — install package first",
                       err=True)
        report["codex_config"] = _patch_codex_config(
            Path.home() / ".codex" / "config.toml", codex_binary,
            db_env=str(ctx.obj["db_path"]) if ctx.obj.get("db_path") else None,
        )

    db_override = str(ctx.obj["db_path"]) if ctx.obj.get("db_path") else None
    editors = {"cursor": cursor, "windsurf": windsurf, "gemini": gemini}
    for agent, wanted in editors.items():
        if not wanted:
            continue
        binary = mcp_binary or _venv_script("skillmem-mcp")
        if not binary.exists():
            click.echo(f"warn: {binary} not found — install package first", err=True)
        report[f"{agent}_config"] = _patch_mcp_servers_json(
            _agent_config_path(MCP_JSON_AGENTS[agent][0]), binary,
            agent=agent, db_env=db_override,
        )

    if opencode:
        binary = mcp_binary or _venv_script("skillmem-mcp")
        if not binary.exists():
            click.echo(f"warn: {binary} not found — install package first", err=True)
        report["opencode_config"] = _patch_opencode_json(
            _agent_config_path(OPENCODE_CONFIG), binary, db_env=db_override,
        )

    click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    click.echo("")
    wired = [name for name, on in (
        ("Claude Code", claude_code), ("Codex", codex), ("Cursor", cursor),
        ("Windsurf", windsurf), ("Gemini CLI", gemini), ("opencode", opencode),
    ) if on]
    if len(wired) > 1:
        if codex and not report.get("codex_config", {}).get("changed", True):
            click.echo("Codex: nothing changed — see codex_config.reason above.", err=True)
            wired.remove("Codex")
        click.echo(f"Done. {', '.join(wired)} — one skill database, "
                   f"{len(wired)} agents.")
    elif codex:
        if report.get("codex_config", {}).get("changed", True):
            click.echo("Done. Open `codex` in any project — the mem_* tools will be there.")
        else:
            click.echo("Nothing changed for Codex — see codex_config.reason above.", err=True)
    elif wired and not claude_code:
        click.echo(f"Done. Open {wired[0]} — the mem_* tools will be there.")
    else:
        click.echo("Done. Open `claude` in any project — the mem_* tools will be there.")
    click.echo("Undo: skillmem uninstall")


@main.command()
@click.option("--claude-code/--no-claude-code", default=True,
              help="Restore ~/.claude.json and remove hooks from settings.json")
@click.option("--codex/--no-codex", default=True,
              help="Remove the skillmem MCP entry from ~/.codex/config.toml")
@click.option("--editors/--no-editors", default=True,
              help="Remove the skillmem MCP entry from Cursor, Windsurf, "
                   "Gemini CLI and opencode configs")
@click.option("--keep-db/--purge-db", default=True,
              help="Keep the SQLite DB (default) or delete it")
@click.pass_context
def uninstall(ctx: click.Context, claude_code: bool, codex: bool,
               editors: bool, keep_db: bool) -> None:
    """Reverse `skillmem init`: remove MCP entry + hook. DB stays unless --purge-db."""
    import time as _time
    report: dict[str, Any] = {"removed": [], "warnings": []}

    if claude_code:
        claude_json = Path.home() / ".claude.json"
        if claude_json.exists():
            try:
                data = json.loads(claude_json.read_text(encoding="utf-8"))
                if "mcpServers" in data and "skillmem" in data["mcpServers"]:
                    backup = claude_json.with_suffix(f".json.bak.{int(_time.time())}")
                    backup.write_text(claude_json.read_text(encoding="utf-8"))
                    del data["mcpServers"]["skillmem"]
                    if not data["mcpServers"]:
                        del data["mcpServers"]
                    _atomic_write_json(claude_json, data)
                    report["removed"].append(f"mcpServers.skillmem (backup: {backup})")
            except json.JSONDecodeError:
                report["warnings"].append(f"could not parse {claude_json}")

        settings_json = Path.home() / ".claude" / "settings.json"
        if settings_json.exists():
            try:
                data = json.loads(settings_json.read_text(encoding="utf-8"))
                changed = False
                for event, groups in list((data.get("hooks") or {}).items()):
                    new_groups = []
                    for grp in groups:
                        new_hooks = [
                            h for h in (grp.get("hooks") or [])
                            if "skillmem" not in (h.get("command") or "")
                        ]
                        if new_hooks:
                            grp["hooks"] = new_hooks
                            new_groups.append(grp)
                        else:
                            changed = True
                    if new_groups:
                        data["hooks"][event] = new_groups
                    else:
                        data["hooks"].pop(event, None)
                        changed = True
                if changed:
                    backup = settings_json.with_suffix(f".json.bak.{int(_time.time())}")
                    backup.write_text(settings_json.read_text(encoding="utf-8"))
                    _atomic_write_json(settings_json, data)
                    report["removed"].append(f"hooks pointing to skillmem (backup: {backup})")
            except json.JSONDecodeError:
                report["warnings"].append(f"could not parse {settings_json}")

    if codex:
        r = _unpatch_codex_config(Path.home() / ".codex" / "config.toml")
        if r.get("changed"):
            report["removed"].append(
                f"mcp_servers.skillmem from config.toml (backup: {r['backup']})")
        elif r.get("reason") and "by hand" in r["reason"]:
            report["warnings"].append(f"codex: {r['reason']}")

    if editors:
        for agent, (parts, label) in MCP_JSON_AGENTS.items():
            r = _unpatch_mcp_servers_json(_agent_config_path(parts))
            if r.get("changed"):
                report["removed"].append(
                    f"mcpServers.skillmem from {label} (backup: {r['backup']})")
        r = _unpatch_opencode_json(_agent_config_path(OPENCODE_CONFIG))
        if r.get("changed"):
            report["removed"].append(
                f"mcp.skillmem from opencode (backup: {r['backup']})")

    # Remove decay/export from the OS scheduler (best-effort).
    try:
        from .schedule import _backend
        removed = _backend()[1]()
        report["removed"] += removed
    except Exception as exc:
        report["warnings"].append(f"schedule remove failed: {exc}")

    if not keep_db:
        db = ctx.obj.get("db_path") or S.default_db_path()
        # the DB's own body files go with it (this DB's namespace only; the
        # docs/ directory is shared by every database under one home)
        try:
            conn = S.connect(db)
            ns = S._db_namespace(conn)
            # legacy (pre-0.11) names carry no namespace: only the ones THIS
            # database references are ours — another DB may use the rest
            mine = {r[0] for r in conn.execute(
                "SELECT body_path FROM memory_items WHERE body_path IS NOT NULL")}
            conn.close()
            for p in S.docs_dir().glob("*.md"):
                m = S._BODY_FILE_RE.search(p.name)
                if m is None:
                    continue
                if m.group("content") is None:
                    if p.name not in mine:
                        continue
                elif (m.group("ns") or "") != (f"-{ns}" if ns else ""):
                    continue
                p.unlink(missing_ok=True)
                report["removed"].append(f"body file {p.name}")
        except Exception as exc:  # noqa: BLE001
            report["warnings"].append(f"body files: {exc}")
        for suffix in ("", "-wal", "-shm"):
            f = db.with_name(db.name + suffix)
            if f.exists():
                f.unlink()
                report["removed"].append(f"DB {f}")

    click.echo(json.dumps(report, ensure_ascii=False, indent=2))


@main.command("tokens-init")
@click.argument("path", type=click.Path(dir_okay=False, path_type=Path))
@click.option(
    "--agents",
    default=(
        "admin:master,agent-a:write_public,agent-b"
    ),
    help="Comma list. Suffix :master for master scope, or :<perm> for a "
         "single named permission (e.g. agent:write_public).",
)
def tokens_init_cmd(path: Path, agents: str) -> None:
    """Generate an agent_tokens.yaml with fresh random bearer tokens."""
    import secrets
    bucket: dict[str, dict[str, Any]] = {}
    for raw in agents.split(","):
        raw = raw.strip()
        if not raw:
            continue
        name, _, modifier = raw.partition(":")
        cfg: dict[str, Any] = {"token": secrets.token_urlsafe(32)}
        if modifier == "master":
            cfg["scope"] = "master"
        elif modifier:
            cfg["permissions"] = [modifier]
        bucket[name] = cfg
    import os as _os
    import yaml as _yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic create with restrictive mode — closes the chmod race window.
    fd = _os.open(str(path), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC, 0o600)
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_yaml.safe_dump(bucket, sort_keys=False, allow_unicode=True))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    click.echo(f"OK: {path} (chmod 0600)")
    click.echo("Edit this file to set per-agent topics, then start the server:")
    click.echo(f"  skillmem-server --tokens {path}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=7000, show_default=True, type=int)
@click.option("--tokens", "tokens_path",
              type=click.Path(dir_okay=False, exists=True, path_type=Path),
              required=True)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int, tokens_path: Path) -> None:
    """Run the FastAPI HTTP server (for multi-agent shared access)."""
    from .server import TokenStore, build_app
    import uvicorn as _uvicorn
    store = TokenStore(tokens_path)
    app = build_app(store, db_path=ctx.obj["db_path"])
    _uvicorn.run(app, host=host, port=port, log_level="info")


# Distribution channel since 0.8.1: private GitHub Releases. Anonymous access
# is a 404 by design — upgrade authenticates with a read-only token. The legacy
# URL mode (self-hosted latest.version + install.sh) still works when
# --url/--version-url or the SKILLMEM_INSTALL_URL / SKILLMEM_VERSION_URL
# environment variables are set.
DEFAULT_INSTALL_URL = ""
DEFAULT_VERSION_URL = ""
DEFAULT_GITHUB_REPO = "liza-studio/skillmem"


def _token_file() -> Path:
    return S.default_data_dir() / "github_token"


def _github_token() -> tuple[str | None, str]:
    """Resolve the GitHub token: env → token file → `gh auth token`."""
    tok = os.environ.get("SKILLMEM_GITHUB_TOKEN")
    if tok:
        return tok.strip(), "env SKILLMEM_GITHUB_TOKEN"
    tf = _token_file()
    if tf.exists():
        tok = tf.read_text(encoding="utf-8").strip()
        if tok:
            return tok, f"file {tf}"
    import shutil as _shutil
    import subprocess as _subprocess
    gh = _shutil.which("gh")
    if gh:
        try:
            proc = _subprocess.run([gh, "auth", "token"], capture_output=True,
                                   text=True, timeout=10)
            tok = proc.stdout.strip()
            if proc.returncode == 0 and tok:
                return tok, "gh auth token"
        except Exception:
            pass
    return None, "not found"


def _gh_get(url: str, token: str, *, accept: str, timeout: int = 30) -> bytes:
    import urllib.request as _urlreq
    req = _urlreq.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "skillmem-upgrade",
    })
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        return resp.read()


@main.group(name="token")
def token_group() -> None:
    """Read-only GitHub token for `skillmem upgrade` (private releases)."""


@token_group.command("set")
@click.argument("value")
def token_set(value: str) -> None:
    """Save the token to a file (chmod 600). The value is never printed."""
    tf = _token_file()
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(value.strip() + "\n", encoding="utf-8")
    try:
        tf.chmod(0o600)  # no-op on Windows; the file lives in the user profile anyway
    except Exception:
        pass
    click.echo(f"token saved → {tf}")


@token_group.command("status")
def token_status() -> None:
    """Show where the token will be taken from (the value itself is never printed)."""
    tok, source = _github_token()
    click.echo(f"token: {'present' if tok else 'MISSING'} ({source})")
    if not tok:
        click.echo("Get one: a fine-grained PAT for the repo with Contents:Read,")
        click.echo("then `skillmem token set <TOKEN>` (or env SKILLMEM_GITHUB_TOKEN).")


@token_group.command("clear")
def token_clear() -> None:
    """Delete the saved token file."""
    tf = _token_file()
    if tf.exists():
        tf.unlink()
        click.echo(f"removed {tf}")
    else:
        click.echo("nothing to clear")


def _version_key(v: str) -> tuple[int, ...]:
    """Parse "0.10.0" into (0, 10, 0) for ordering.

    String comparison would rank "0.10.0" below "0.6.0" — correct only while
    every component stays single-digit. Unparsable components sort as 0.
    """
    parts = []
    for chunk in v.strip().split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _upgrade_via_github(check_only: bool, repo: str, token_opt: str | None) -> None:
    """GitHub Releases channel: authenticated check + offline reinstall."""
    import hashlib as _hashlib
    import os as _os
    import tempfile as _tempfile
    from . import __version__

    token = token_opt or _github_token()[0]
    if not token:
        click.echo("no GitHub token — private releases need auth.", err=True)
        click.echo("Fix: `skillmem token set <TOKEN>` (fine-grained PAT, "
                   "Contents:Read on the repo) or env SKILLMEM_GITHUB_TOKEN, "
                   "or `gh auth login`.", err=True)
        sys.exit(2)

    api = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        release = json.loads(_gh_get(api, token, accept="application/vnd.github+json"))
    except Exception as exc:
        click.echo(f"could not fetch {api}: {exc}", err=True)
        sys.exit(1)

    latest = str(release.get("tag_name") or "").lstrip("v")
    current = __version__
    click.echo(f"installed: {current}")
    click.echo(f"latest:    {latest or 'unknown'} (github.com/{repo})")
    if not latest:
        sys.exit(1)
    if current == latest:
        click.echo("✓ up to date")
        return
    if _version_key(latest) <= _version_key(current):
        click.echo(f"warn: installed {current} is newer than latest {latest}", err=True)
        return
    if check_only:
        click.echo(f"upgrade available: {current} → {latest}")
        click.echo("Run `skillmem upgrade` to install.")
        return

    assets = {a["name"]: a for a in release.get("assets", [])}
    tarball_name = next(
        (n for n in assets if n.endswith(".tar.gz") and not n.endswith(".sha256")), None)
    installer_name = "install.ps1" if sys.platform == "win32" else "install.sh"
    if not tarball_name or installer_name not in assets:
        click.echo(f"release v{latest} is missing assets "
                   f"(need <pkg>.tar.gz + {installer_name}; "
                   f"have: {', '.join(assets) or 'none'})", err=True)
        sys.exit(1)

    click.echo(f"upgrading {current} → {latest}")
    tmp = Path(_tempfile.mkdtemp(prefix="skillmem-upgrade-"))

    def _asset(name: str) -> Path:
        data = _gh_get(assets[name]["url"], token,
                       accept="application/octet-stream", timeout=120)
        p = tmp / name
        p.write_bytes(data)
        return p

    tarball = _asset(tarball_name)
    sha_name = f"{tarball_name}.sha256"
    if sha_name in assets:
        sha_file = _asset(sha_name)
        expected = sha_file.read_text(encoding="utf-8").split()[0].lower()
        actual = _hashlib.sha256(tarball.read_bytes()).hexdigest()
        if expected != actual:
            click.echo(f"SHA256 mismatch! expected={expected} actual={actual}", err=True)
            sys.exit(1)
        click.echo(f"checksum verified ({expected})")
    else:
        click.echo("warn: no .sha256 asset — proceeding without verification", err=True)
    installer = _asset(installer_name)

    click.echo("Re-executing installer in place...")
    if sys.platform == "win32":
        _os.execvp("powershell", [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(installer), "-From", str(tarball), "-NoClaudeCode",
        ])  # noqa: never returns
    _os.execvp("bash", [
        "bash", str(installer), f"--from={tarball}", "--no-claude-code",
    ])  # noqa: never returns


@main.command()
@click.option("--check", "check_only", is_flag=True,
              help="Only report current vs latest, don't upgrade.")
@click.option("--repo", default=None,
              help=f"GitHub repo for releases (default: {DEFAULT_GITHUB_REPO})")
@click.option("--token", "token_opt", default=None,
              help="GitHub token override (default: skillmem token status)")
@click.option("--url", "install_url", default=None,
              help="Legacy: self-hosted install.sh URL")
@click.option("--version-url", "version_url", default=None,
              help="Legacy: self-hosted latest.version URL")
def upgrade(check_only: bool, repo: str | None, token_opt: str | None,
            install_url: str | None, version_url: str | None) -> None:
    """Check for and pull the latest release (GitHub Releases).

    --check just compares versions; without it, we re-execute the installer
    in-place (current binary is replaced via ``os.execvp``)."""
    import os as _os
    import urllib.request as _urlreq
    from . import __version__

    install_url = install_url or _os.environ.get("SKILLMEM_INSTALL_URL", DEFAULT_INSTALL_URL)
    version_url = version_url or _os.environ.get("SKILLMEM_VERSION_URL", DEFAULT_VERSION_URL)

    # GitHub is the primary channel; the legacy URL mode only when explicitly configured.
    if not version_url:
        _upgrade_via_github(
            check_only,
            repo or _os.environ.get("SKILLMEM_GITHUB_REPO", DEFAULT_GITHUB_REPO),
            token_opt,
        )
        return
    for label, url in (("--version-url", version_url), ("--url", install_url)):
        if url and not url.startswith("https://"):
            click.echo(f"{label} must be an https:// URL, got: {url}", err=True)
            sys.exit(2)

    current = __version__
    latest = None
    try:
        with _urlreq.urlopen(version_url, timeout=5) as resp:
            latest = resp.read().decode("utf-8").strip()
    except Exception as exc:
        click.echo(f"warn: could not fetch {version_url}: {exc}", err=True)

    click.echo(f"installed: {current}")
    click.echo(f"latest:    {latest or 'unknown'}")

    if latest is None:
        sys.exit(1 if check_only else 0)

    if current == latest:
        click.echo("✓ up to date")
        return
    if _version_key(latest) <= _version_key(current):
        click.echo(f"warn: installed {current} is newer than latest {latest}", err=True)
        return
    if check_only:
        click.echo(f"upgrade available: {current} → {latest}")
        click.echo("Run `skillmem upgrade` to install.")
        return

    if not install_url:
        click.echo("no --url/SKILLMEM_INSTALL_URL configured; cannot install", err=True)
        sys.exit(2)

    click.echo(f"upgrading {current} → {latest}")
    click.echo("Re-executing installer in place...")
    # Download first, then exec the file. The old form built a shell string
    # ("curl -sSL {url} | bash"), so any shell metacharacter in a URL taken from
    # an env var or --url ran as a command. No shell is involved now.
    import tempfile as _tempfile

    if sys.platform == "win32":
        # Distribution convention: install.ps1 sits next to install.sh.
        if install_url.endswith("install.sh"):
            install_url = install_url[: -len("install.sh")] + "install.ps1"
        with _urlreq.urlopen(install_url, timeout=30) as resp:
            script = resp.read()
        with _tempfile.NamedTemporaryFile("wb", suffix=".ps1", delete=False) as fh:
            fh.write(script)
            script_path = fh.name
        _os.execvp("powershell", [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", script_path, "-NoClaudeCode",
        ])  # noqa: never returns

    with _urlreq.urlopen(install_url, timeout=30) as resp:
        script = resp.read()
    with _tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as fh:
        fh.write(script)
        script_path = fh.name
    _os.chmod(script_path, 0o700)
    # execvp replaces this process — the old binary is safe to overwrite once
    # we've handed off to the installer.
    _os.execvp("bash", ["bash", script_path, "--no-claude-code"])  # noqa: never returns


@main.command()
@click.option("--strict", is_flag=True, help="Exit non-zero on any chain break.")
@click.pass_context
def verify(ctx: click.Context, strict: bool) -> None:
    """Verify the SHA256 hash-chain over memory_history (tamper-evidence)."""
    conn = _conn(ctx.obj["db_path"])
    checked, breaks = S.verify_history(conn)
    click.echo(f"checked {checked} history rows")
    if not breaks:
        click.echo("OK: chain intact")
        return
    click.echo(f"BROKEN: {len(breaks)} chain mismatches", err=True)
    for b in breaks[:10]:
        click.echo(
            f"  row {b.row_id} slug={b.slug} changed_at={b.changed_at} "
            f"expected_self={b.expected_self[:12]}… actual={b.actual_self or 'NULL'}",
            err=True,
        )
    if strict:
        sys.exit(1)


@main.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Show DB stats and basic health."""
    path = ctx.obj["db_path"] or S.default_db_path()
    conn = _conn(path)
    info = {
        "db_path": str(path),
        "schema_version": conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"],
        **S.stats(conn),
        "lexical_reindex_pending": S.lexical_reindex_pending(conn),
        "semantic": _semantic_report(),
    }
    click.echo(json.dumps(info, ensure_ascii=False, indent=2))


def _semantic_report() -> dict:
    """Report whether vector recall is actually live.

    The embedding layer degrades to BM25 in silence by design, so a broken
    model cache looks identical to a healthy install unless we say so here.
    Loading the model is the only honest check — a cheap import is not enough
    (weights live outside the package and the OS can purge them).
    """
    from . import embed

    report: dict = {"model": embed.MODEL_NAME, "cache_dir": embed.model_cache_dir()}
    if not embed.semantic_enabled():
        report["status"] = "off (MEM_SEMANTIC=0)"
        return report
    try:
        import fastembed  # noqa: F401
    except Exception:
        report["status"] = "off — fastembed not installed"
        report["hint"] = "reinstall without --no-semantic, or: uv pip install 'skillmem[semantic]'"
        return report
    if embed.available():
        report["status"] = "on"
    else:
        report["status"] = "DEGRADED — fastembed present but model failed to load; recall is BM25-only"
        report["hint"] = f"check/clear {embed.model_cache_dir()} and re-run to re-download (~220 MB)"
    return report


# --------------------------------------------------------------------------- #
# skill learning commands
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("slug")
@click.option("--title", "-t", required=True, help="Short skill title.")
@click.option("--trigger", required=True, help="What situation triggers this skill.")
@click.option("--steps", required=True, help="Steps taken.")
@click.option("--outcome", required=True, type=click.Choice(["success", "partial", "failure"]))
@click.option("--lessons", default=None, help="What to do differently next time.")
@click.option("--project", default=None)
@click.option("--tags", default=None, help="Comma-separated tags.")
@click.pass_context
def learn(
    ctx: click.Context,
    slug: str,
    title: str,
    trigger: str,
    steps: str,
    outcome: str,
    lessons: str | None,
    project: str | None,
    tags: str | None,
) -> None:
    """Record an after-action skill from task experience."""
    conn = _conn(ctx.obj["db_path"])
    trusted_at, trusted_by = _owner_trust()
    item = S.MemoryItem(
        slug=slug,
        kind="skill",
        title=title,
        body=S.skill_body(trigger, steps, outcome, lessons),
        origin="owner" if trusted_at else "agent",
        trusted_at=trusted_at, trusted_by=trusted_by,
        project=project,
        tags=[t.strip() for t in tags.split(",")] if tags else [],
        visibility="public",
    )
    try:
        result = S.upsert(conn, item, links=S.extract_wikilinks(item.body))
    except (S.MemoryConflict, ValueError) as exc:
        click.echo(f"CONFLICT: {exc}", err=True)
        sys.exit(1)
    click.echo(f"Learned: {result.slug} (id={result.id})")


@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=5, type=int)
@click.option("--no-reinforce", is_flag=True, help="Don't bump strength on retrieval.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (text for humans, json for hooks).",
)
@click.pass_context
def recall(ctx: click.Context, query: str, limit: int, no_reinforce: bool, fmt: str) -> None:
    """Find relevant skills for a task (Ebbinghaus-weighted BM25)."""
    conn = _conn(ctx.obj["db_path"])
    results = S.recall_skills(conn, query, limit=limit, auto_reinforce=not no_reinforce)
    from .hooks import frame_for_model
    for r in results:
        frame_for_model(r, r)  # unapproved skills travel inside the frame, JSON or text
    if fmt == "json":
        click.echo(json.dumps(results, ensure_ascii=False, default=str))
        return
    if not results:
        click.echo("No skills found.")
        return
    for r in results:
        strength_bar = "█" * int(r["strength"] * 5)
        click.echo(
            f"  [{r['slug']}] {r['title']}\n"
            f"    strength={r['strength']:.2f} {strength_bar}  "
            f"access={r['access_count']}  {r['freshness']}"
        )
        if r.get("body"):
            lines = r["body"].split("\n")
            # a framed body must keep both markers, or the frame is decapitated
            for line in (lines if r.get("trusted") is False else lines[:4]):
                click.echo(f"    {line}")
        click.echo()


@main.command("skills-top")
@click.option("--limit", "-n", default=50, type=int)
@click.pass_context
def skills(ctx: click.Context, limit: int) -> None:
    """List skills with strength and access count (was `skills`, which the
    `skills` pack group shadowed — the command was unreachable)."""
    conn = _conn(ctx.obj["db_path"])
    items = S.list_items(conn, kind="skill", limit=limit)
    if not items:
        click.echo("No skills yet.")
        return
    for item in items:
        strength_bar = "█" * int(item.strength * 5)
        click.echo(
            f"  [{item.slug}] {item.title}\n"
            f"    strength={item.strength:.2f} {strength_bar}  "
            f"access={item.access_count}  "
            f"created={item.created_at}"
        )


@main.command()
@click.option("--days", default=14, type=int, help="Threshold in days for decay.")
@click.pass_context
def decay(ctx: click.Context, days: int) -> None:
    """Run Ebbinghaus decay on unused skills."""
    conn = _conn(ctx.obj["db_path"])
    # The nightly job is where a minute of CPU is affordable: v11 needs the lexical
    # index rebuilt, and doing it in a hook would blow the hook's timeout.
    if S.lexical_reindex_pending(conn):
        n = S.restem_all(conn)
        click.echo(f"Rebuilt the lexical index for {n} memories (v11).")
    decayed = S.decay_stale(conn, days_threshold=days)
    if not decayed:
        click.echo("Nothing to decay.")
    for d in decayed:
        click.echo(f"  {d['slug']}: {d['old_strength']:.3f} → {d['new_strength']:.3f}")
    if decayed:
        click.echo(f"Decayed {len(decayed)} skills.")
    # Lifecycle sweep rides on the same scheduled run (active -> stale -> archived).
    # It runs whether or not anything decayed: once idle skills sit at the
    # floor there is nothing left to decay, and that is exactly when they
    # should be archived — an early return here meant nothing ever was.
    sweep = S.sweep_lifecycle(conn)
    gc = S.gc_body_files(conn)
    if gc:
        click.echo(f"Removed {gc} orphaned body files.")
    if sweep["staled"]:
        click.echo(f"Marked stale: {', '.join(sweep['staled'])}")
    if sweep["archived"]:
        click.echo(f"Archived (backed up): {', '.join(sweep['archived'])}")


@main.command()
@click.argument("slug")
@click.option("--evidence", type=click.Choice(sorted(S.EVIDENCE_WEIGHTS)),
              default="self_report", show_default=True,
              help="What confirms the outcome. Only outside evidence moves strength.")
@click.pass_context
def reinforce(ctx: click.Context, slug: str, evidence: str) -> None:
    """Record how a skill turned out (mirrors mem_reinforce).

    Strength rises only on evidence from outside the agent's own judgement;
    the default `self_report` refreshes recency without rewarding anything.
    """
    conn = _conn(ctx.obj["db_path"])
    result = S.reinforce(conn, slug, evidence=evidence)
    if not result:
        click.echo(f"not found: {slug}", err=True)
        sys.exit(1)
    click.echo(
        f"Reinforced: {result['slug']} strength={result['strength']:.2f} "
        f"access={result['access_count']} evidence={result['evidence']}"
    )


@main.command()
@click.argument("slug")
@click.option("--off", is_flag=True, help="Unpin instead: put it back under decay.")
@click.pass_context
def pin(ctx: click.Context, slug: str, off: bool) -> None:
    """Exempt a skill from decay and archiving (mirrors mem_pin).

    For a rule that matters precisely because it is rarely needed — a deploy
    gate, a safety constraint — where being unused is not evidence of being
    useless.
    """
    conn = _conn(ctx.obj["db_path"])
    result = S.set_pinned(conn, slug, not off)
    if not result:
        click.echo(f"not found: {slug}", err=True)
        sys.exit(1)
    state = "pinned" if result["pinned"] else "unpinned"
    click.echo(f"{state}: {slug}" + ("" if result["changed"] else " (already)"))


@main.group()
def skills() -> None:
    """Import and manage third-party skill packs (ponytail, unlazy, ...)."""


@skills.command("add")
@click.argument("source")
@click.option("--name", default=None, help="Override the pack name.")
@click.option("--dry-run", is_flag=True, help="List what would be imported.")
@click.pass_context
def skills_add(ctx: click.Context, source: str, name: str | None, dry_run: bool) -> None:
    """Import a skill pack from a repo (owner/repo), a git URL, or a local path.

    Only SKILL.md files are read — nothing from the pack is executed. Imported
    skills carry their origin and licence, are tagged untrusted-origin, and
    from then on live by the ordinary rules: recalled when relevant, confirmed
    by outside evidence, faded out when they never help.
    """
    from .packs import PackError, import_pack

    conn = _conn(ctx.obj["db_path"])
    try:
        report = import_pack(conn, source, pack_name=name, dry_run=dry_run)
    except PackError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    click.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    verb = "would import" if dry_run else "imported"
    click.echo(f"\n{verb} {len(report.imported)} skills from {report.pack}"
               + (f" (licence: {report.license})" if report.license else ""))


@skills.command("ls")
@click.pass_context
def skills_ls(ctx: click.Context) -> None:
    """List imported packs and how well each is holding up."""
    from .packs import list_packs

    rows = list_packs(_conn(ctx.obj["db_path"]))
    if not rows:
        click.echo("(no packs imported)")
        return
    click.echo(f"{'pack':<24}{'skills':>7}{'strength':>10}{'confirmed':>11}"
               f"{'failures':>10}{'archived':>10}")
    for r in rows:
        click.echo(f"{r['pack']:<24}{r['skills']:>7}{r['avg_strength']:>10.2f}"
                   f"{r['confirmed']:>11}{r['failures']:>10}{r['archived']:>10}")


@skills.command("rm")
@click.argument("pack")
@click.option("--reason", default="pack removed", show_default=True)
@click.pass_context
def skills_rm(ctx: click.Context, pack: str, reason: str) -> None:
    """Remove every skill imported from a pack (soft delete, history kept)."""
    from .packs import remove_pack

    removed = remove_pack(_conn(ctx.obj["db_path"]), pack, reason=reason)
    if not removed:
        click.echo(f"no skills found for pack: {pack}", err=True)
        sys.exit(1)
    click.echo(f"removed {len(removed)} skills from {pack}")


@main.command("mcp")
def mcp_cmd() -> None:
    """Run the MCP stdio server (registry clients launch `uvx skillmem mcp`)."""
    from .mcp_server import run as _mcp_run
    _mcp_run()


@main.command("skills-lifecycle")
@click.pass_context
def skills_lifecycle(ctx: click.Context) -> None:
    """Show skill counts per lifecycle state (active/stale/archived)."""
    conn = _conn(ctx.obj["db_path"])
    counts = S.lifecycle_counts(conn)
    if not counts:
        click.echo("No skills yet.")
        return
    for state in ("active", "stale", "archived"):
        click.echo(f"  {state:9} {counts.get(state, 0)}")


@main.command("skills-restore")
@click.argument("slug")
@click.pass_context
def skills_restore(ctx: click.Context, slug: str) -> None:
    """Restore an archived/stale skill back to active."""
    conn = _conn(ctx.obj["db_path"])
    if S.restore_skill(conn, slug):
        click.echo(f"Restored '{slug}' → active.")
    else:
        click.echo(f"Skill '{slug}' not found.")


@main.command("skills-dups")
@click.option("--threshold", default=0.85, type=float, help="Cosine threshold.")
@click.pass_context
def skills_dups(ctx: click.Context, threshold: float) -> None:
    """List near-duplicate skill pairs (curator candidates, read-only)."""
    conn = _conn(ctx.obj["db_path"])
    pairs = S.find_duplicate_skills(conn, threshold=threshold)
    if not pairs:
        click.echo("No duplicate candidates.")
        return
    for p in pairs:
        click.echo(f"  {p['cosine']:.3f}  {p['a']} (s={p['a_strength']:.2f})  ⟷  "
                   f"{p['b']} (s={p['b_strength']:.2f})")
    click.echo(f"{len(pairs)} candidate pair(s).")


@main.command("reindex-embeddings")
@click.option("--all", "all_rows", is_flag=True, help="Re-embed every row, not just missing.")
@click.pass_context
def reindex_embeddings(ctx: click.Context, all_rows: bool) -> None:
    """Backfill semantic embeddings for stored memories (needs fastembed)."""
    conn = _conn(ctx.obj["db_path"])
    res = S.reindex_embeddings(conn, only_missing=not all_rows)
    if res.get("unavailable"):
        click.echo("Embedder unavailable (fastembed not installed or MEM_SEMANTIC=0).")
        return
    click.echo(f"Embedded {res['updated']} of {res.get('total', 0)} rows.")


from .hooks import hook_group  # noqa: E402 — click groups defined after main
from .schedule import schedule_group  # noqa: E402

main.add_command(hook_group)
main.add_command(schedule_group)


if __name__ == "__main__":
    main()
