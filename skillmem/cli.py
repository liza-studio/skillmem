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
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format (text for humans, json for hooks).",
)
@click.pass_context
def search(
    ctx: click.Context, query: str, kind: str | None, project: str | None, limit: int, fmt: str
) -> None:
    """Full-text search via FTS5 BM25."""
    conn = _conn(ctx.obj["db_path"])
    hits = S.search(conn, query, kind=kind, project=project, limit=limit)
    if fmt == "json":
        click.echo(json.dumps(hits, ensure_ascii=False, default=str))
        return
    if not hits:
        click.echo("(no results)")
        return
    for h in hits:
        snippet = (h.get("snippet") or "").replace("\n", " ")
        rank = h.get("rank")
        click.echo(f"[{h['kind']:<9}] {h['slug']}  (rank={rank:.2f})")
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
    if item.source_session:
        click.echo(f"source_session={item.source_session}")
    if item.body_path:
        click.echo(f"body_path={item.body_path}")
    click.echo("")
    click.echo(S.load_body(item))
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
        click.echo(f"[{it.kind:<9}] {it.slug}  — {it.title}")


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
              help="Refuse near-duplicates (Jaccard > 0.7)")
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

    item = S.MemoryItem(
        slug=slug, kind=kind, title=title, body=body_text,
        project=project, agent=agent, ttl_days=ttl_days,
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
    click.echo("\n".join(lines))


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
    """Atomic write: tempfile in same dir + os.replace. Never half-written."""
    import os as _os, tempfile as _tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        _os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
        return {"changed": False, "reason": "skillmem MCP already configured",
                "backup": str(backup) if backup else None}

    entry: dict[str, Any] = {"command": str(mcp_binary), "args": []}
    if db_env:
        entry["env"] = {"SKILLMEM_DB": db_env}
    servers["skillmem"] = entry
    _atomic_write_json(claude_json, data)
    return {"changed": True, "added": "mcpServers.skillmem",
            "backup": str(backup) if backup else None}


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


@main.command()
@click.option("--claude-code", is_flag=True,
              help="Configure MCP entry in ~/.claude.json and add Stop hook")
@click.option("--migrate-existing/--skip-migrate", default=True,
              help="Auto-discover and import all ~/.claude/projects/*/memory")
@click.option("--mcp-binary", type=click.Path(path_type=Path), default=None,
              help="Override path to skillmem-mcp (default: auto-detect)")
@click.option("--hooks", "hooks_mode",
              type=click.Choice(["full", "minimal", "none"]), default="full",
              help="full: recall/recap/guard hooks + migrate; "
                   "minimal: only Stop→migrate; none: no hooks")
@click.pass_context
def init(
    ctx: click.Context,
    claude_code: bool,
    migrate_existing: bool,
    mcp_binary: Path | None,
    hooks_mode: str,
) -> None:
    """First-time setup: create DB, migrate auto-memory, wire up Claude Code."""
    report: dict[str, Any] = {}

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
            db_env=str(db_path) if str(db_path) != str(S.default_db_path()) else None,
        )

        settings_json = Path.home() / ".claude" / "settings.json"
        skillmem_bin = _venv_script("skillmem")

        def _hook(event: str, args: list[str], **kw: Any) -> dict[str, Any]:
            return _patch_settings_hook(settings_json, skillmem_bin,
                                        event=event, args=args, **kw)

        hook_reports: list[dict[str, Any]] = []
        if hooks_mode != "none":
            hook_reports.append(_hook("Stop", ["migrate"]))
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
            ]
        report["hooks"] = hook_reports

    click.echo(json.dumps(report, ensure_ascii=False, indent=2))
    click.echo("")
    click.echo("Done. Open `claude` in any project — the mem_* tools will be there.")
    click.echo("Undo: skillmem uninstall")


@main.command()
@click.option("--claude-code", is_flag=True, default=True,
              help="Restore ~/.claude.json and remove hooks from settings.json")
@click.option("--keep-db/--purge-db", default=True,
              help="Keep the SQLite DB (default) or delete it")
@click.pass_context
def uninstall(ctx: click.Context, claude_code: bool, keep_db: bool) -> None:
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
                    claude_json.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
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
                    settings_json.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    report["removed"].append(f"hooks pointing to skillmem (backup: {backup})")
            except json.JSONDecodeError:
                report["warnings"].append(f"could not parse {settings_json}")

    # Remove decay/export from the OS scheduler (best-effort).
    try:
        from .schedule import _backend
        removed = _backend()[1]()
        report["removed"] += removed
    except Exception as exc:
        report["warnings"].append(f"schedule remove failed: {exc}")

    if not keep_db:
        db = S.default_db_path()
        if db.exists():
            db.unlink()
            report["removed"].append(f"DB {db}")

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
    item = S.MemoryItem(
        slug=slug,
        kind="skill",
        title=title,
        body=S.skill_body(trigger, steps, outcome, lessons),
        project=project,
        tags=[t.strip() for t in tags.split(",")] if tags else [],
        visibility="public",
    )
    try:
        result = S.upsert(conn, item, links=S.extract_wikilinks(item.body))
    except S.MemoryConflict as exc:
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
            for line in r["body"].split("\n")[:4]:
                click.echo(f"    {line}")
        click.echo()


@main.command()
@click.option("--limit", "-n", default=50, type=int)
@click.pass_context
def skills(ctx: click.Context, limit: int) -> None:
    """List all skills with strength and access count."""
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
    decayed = S.decay_stale(conn, days_threshold=days)
    if not decayed:
        click.echo("Nothing to decay.")
        return
    for d in decayed:
        click.echo(f"  {d['slug']}: {d['old_strength']:.3f} → {d['new_strength']:.3f}")
    click.echo(f"Decayed {len(decayed)} skills.")
    # Lifecycle sweep rides on the same scheduled run (active -> stale -> archived).
    sweep = S.sweep_lifecycle(conn)
    if sweep["staled"]:
        click.echo(f"Marked stale: {', '.join(sweep['staled'])}")
    if sweep["archived"]:
        click.echo(f"Archived (backed up): {', '.join(sweep['archived'])}")


@main.command()
@click.argument("slug")
@click.pass_context
def reinforce(ctx: click.Context, slug: str) -> None:
    """Explicitly reinforce a skill after it proved useful (mirrors mem_reinforce)."""
    conn = _conn(ctx.obj["db_path"])
    result = S.reinforce(conn, slug)
    if not result:
        click.echo(f"not found: {slug}", err=True)
        sys.exit(1)
    click.echo(
        f"Reinforced: {result['slug']} strength={result['strength']:.2f} "
        f"access={result['access_count']}"
    )


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
