"""MCP stdio server exposing skillmem as 8 tools.

Tools:
    mem_search    — hybrid full-text search (FTS5 BM25 + optional vector recall)
    mem_get       — fetch a memory by slug (with history + links)
    mem_write     — insert a new memory (refuses silent overwrites)
    mem_update    — update an existing memory (requires reason)
    mem_list      — list memories by kind/project, most-recent first
    mem_learn     — record an after-action skill (trigger/steps/outcome/lessons)
    mem_recall    — find relevant skills for a task, strength-weighted
    mem_reinforce — bump a skill's strength after it proved useful

Designed to be wired into ~/.claude.json under mcpServers.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

import sqlite3

from . import storage as S


SERVER_NAME = "skillmem"


def _db_path() -> Path:
    override = os.environ.get("SKILLMEM_DB")
    return Path(override).expanduser() if override else S.default_db_path()



_CONN: "sqlite3.Connection | None" = None


def _shared_conn() -> "sqlite3.Connection":
    """One connection for the life of the stdio server.

    Each tool call used to open its own connection and drop it on the floor;
    over a long session that is a pile of WAL readers held open by refcount
    alone. The MCP server is a single process serving one client, so a single
    cached connection is both correct and cheaper.
    """
    global _CONN
    if _CONN is None:
        _CONN = S.connect(_db_path())
        S.init_schema(_CONN)
    return _CONN

def _ok(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


def _err(message: str) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps({"error": message}, ensure_ascii=False))]


# --------------------------------------------------------------------------- #
# tool implementations
# --------------------------------------------------------------------------- #


def _tool_search(args: dict[str, Any]) -> list[TextContent]:
    query = (args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    conn = _shared_conn()
    S.init_schema(conn)
    hits = S.search(
        conn,
        query,
        kind=args.get("kind") or None,
        project=args.get("project") or None,
        limit=int(args.get("limit") or 10),
    )
    summary = [
        {
            "slug": h["slug"],
            "kind": h["kind"],
            "title": h["title"],
            "project": h["project"],
            "rank": h["rank"],
            "snippet": h.get("snippet"),
            "updated_at": h["updated_at"],
        }
        for h in hits
    ]
    return _ok({"count": len(summary), "results": summary})


def _tool_get(args: dict[str, Any]) -> list[TextContent]:
    slug = args.get("slug")
    if not slug:
        return _err("slug is required")
    conn = _shared_conn()
    S.init_schema(conn)
    item = S.get(conn, slug)
    if not item:
        return _err(f"not found: {slug}")
    payload = item.to_dict()
    payload["body"] = S.load_body(item)  # materialize external bodies
    payload["links_out"] = S.links_from(conn, slug)
    payload["links_in"] = S.links_to(conn, slug)
    if args.get("include_history"):
        payload["history"] = S.history(conn, slug)
    return _ok(payload)


def _tool_list(args: dict[str, Any]) -> list[TextContent]:
    conn = _shared_conn()
    S.init_schema(conn)
    items = S.list_items(
        conn,
        kind=args.get("kind") or None,
        project=args.get("project") or None,
        limit=int(args.get("limit") or 50),
    )
    summary = [
        {"slug": i.slug, "kind": i.kind, "title": i.title,
         "project": i.project, "updated_at": i.updated_at}
        for i in items
    ]
    return _ok({"count": len(summary), "items": summary})


_MCP_AGENT = os.environ.get("SKILLMEM_AGENT", "claude-code")


def _tool_write(args: dict[str, Any]) -> list[TextContent]:
    required = ("slug", "title", "body")
    for r in required:
        if not args.get(r):
            return _err(f"{r} is required")

    conn = _shared_conn()
    S.init_schema(conn)
    item = S.MemoryItem(
        slug=args["slug"],
        kind=args.get("kind") or "note",
        title=args["title"],
        body=args["body"],
        project=args.get("project"),
        # Agent identity is set server-side — clients can't forge authorship.
        agent=_MCP_AGENT,
        tags=list(args.get("tags") or []),
        topics=list(args.get("topics") or []),
        ttl_days=args.get("ttl_days"),
    )
    try:
        result = S.upsert(
            conn, item,
            check_conflicts=bool(args.get("check_conflicts", True)),
            links=S.extract_wikilinks(item.body),
        )
    except S.MemoryConflict as exc:
        return _err(str(exc))
    return _ok({"ok": True, "slug": result.slug, "id": result.id, "kind": result.kind})


def _tool_update(args: dict[str, Any]) -> list[TextContent]:
    slug = args.get("slug")
    body = args.get("body")
    reason = args.get("reason")
    if not (slug and body and reason):
        return _err("slug, body, and reason are required")
    conn = _shared_conn()
    S.init_schema(conn)
    existing = S.get(conn, slug)
    if not existing:
        return _err(f"not found: {slug}")

    existing.body = body
    if args.get("title"):
        existing.title = args["title"]
    if args.get("kind"):
        existing.kind = args["kind"]
    if args.get("project") is not None:
        existing.project = args["project"]
    existing.agent = _MCP_AGENT
    if args.get("tags") is not None:
        existing.tags = list(args["tags"])
    if args.get("topics") is not None:
        existing.topics = list(args["topics"])

    result = S.upsert(
        conn, existing, reason=reason,
        links=S.extract_wikilinks(body),
    )
    return _ok({"ok": True, "slug": result.slug, "history_entries": len(S.history(conn, slug))})


def _tool_learn(args: dict[str, Any]) -> list[TextContent]:
    """Record an after-action skill from task experience."""
    required = ("slug", "title", "trigger", "steps", "outcome")
    for r in required:
        if not args.get(r):
            return _err(f"{r} is required")

    body_parts = [
        f"**trigger:** {args['trigger']}",
        f"**steps:** {args['steps']}",
        f"**outcome:** {args['outcome']}",
    ]
    if args.get("lessons"):
        body_parts.append(f"**lessons:** {args['lessons']}")

    conn = _shared_conn()
    S.init_schema(conn)
    item = S.MemoryItem(
        slug=args["slug"],
        kind="skill",
        title=args["title"],
        body="\n".join(body_parts),
        project=args.get("project"),
        agent=_MCP_AGENT,
        tags=list(args.get("tags") or []),
        topics=list(args.get("topics") or []),
        visibility=args.get("visibility") or "public",
        ttl_days=args.get("ttl_days"),
    )
    try:
        result = S.upsert(
            conn, item,
            check_conflicts=bool(args.get("check_conflicts", True)),
            links=S.extract_wikilinks(item.body),
        )
    except S.MemoryConflict as exc:
        return _err(str(exc))
    return _ok({"ok": True, "slug": result.slug, "id": result.id, "kind": "skill"})


def _tool_recall(args: dict[str, Any]) -> list[TextContent]:
    """Find relevant skills before starting a task."""
    query = (args.get("query") or "").strip()
    if not query:
        return _err("query is required")
    conn = _shared_conn()
    S.init_schema(conn)
    results = S.recall_skills(
        conn, query,
        limit=int(args.get("limit") or 5),
        auto_reinforce=bool(args.get("auto_reinforce", True)),
    )
    return _ok({"count": len(results), "skills": results})


def _tool_reinforce(args: dict[str, Any]) -> list[TextContent]:
    """Explicitly reinforce a skill after successful use."""
    slug = args.get("slug")
    if not slug:
        return _err("slug is required")
    conn = _shared_conn()
    S.init_schema(conn)
    result = S.reinforce(conn, slug)
    if not result:
        return _err(f"not found: {slug}")
    return _ok(result)


# --------------------------------------------------------------------------- #
# tool descriptors
# --------------------------------------------------------------------------- #

TOOLS: list[Tool] = [
    Tool(
        name="mem_search",
        description=(
            "Full-text search across skillmem memory. Uses FTS5 BM25 over titles "
            "+ bodies + tags + topics with English and Russian Snowball stemming. "
            "Returns top-N results with rank and snippet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "kind": {
                    "type": "string",
                    "description": "Optional filter: feedback / project / reference / user / note.",
                },
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="mem_get",
        description=(
            "Fetch one memory by slug. Returns full body, provenance "
            "(created_at, updated_at, source_session), wikilinks in/out. "
            "Set include_history=true to get the version trail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "include_history": {"type": "boolean", "default": False},
            },
            "required": ["slug"],
        },
    ),
    Tool(
        name="mem_list",
        description=(
            "List memories most-recent-first. Optionally filter by kind or project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "project": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="mem_write",
        description=(
            "Insert a new memory. Slug must be unique. To overwrite an existing "
            "slug, use mem_update with a reason — mem_write deliberately refuses "
            "silent overwrites to preserve record provenance."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "kind": {"type": "string", "default": "note"},
                "project": {"type": "string"},
                "agent": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "topics": {"type": "array", "items": {"type": "string"}},
                "ttl_days": {"type": "integer"},
                "check_conflicts": {"type": "boolean", "default": True,
                    "description": "Reject if Jaccard word overlap > 0.7 with an existing memory."},
            },
            "required": ["slug", "title", "body"],
        },
    ),
    Tool(
        name="mem_update",
        description=(
            "Update an existing memory. Old body is preserved in memory_history "
            "with the supplied reason — every record keeps a full "
            "birth/expiration/death trail."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "body": {"type": "string"},
                "reason": {"type": "string", "description": "Why this update was made."},
                "title": {"type": "string"},
                "kind": {"type": "string"},
                "project": {"type": "string"},
                "agent": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "topics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["slug", "body", "reason"],
        },
    ),
    Tool(
        name="mem_learn",
        description=(
            "Record an after-action skill from task experience. The agent writes "
            "what triggered the task, what steps were taken, the outcome, and "
            "lessons learned. Stored as kind=skill with Ebbinghaus strength tracking."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Unique slug like 'skill-deploy-nginx'."},
                "title": {"type": "string", "description": "Short skill title."},
                "trigger": {"type": "string", "description": "What situation triggers this skill."},
                "steps": {"type": "string", "description": "Steps taken to complete the task."},
                "outcome": {"type": "string", "description": "Result: success/partial/failure."},
                "lessons": {"type": "string", "description": "What to do differently next time."},
                "project": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "topics": {"type": "array", "items": {"type": "string"}},
                "visibility": {"type": "string", "default": "public"},
                "ttl_days": {"type": "integer"},
                "check_conflicts": {"type": "boolean", "default": True},
            },
            "required": ["slug", "title", "trigger", "steps", "outcome"],
        },
    ),
    Tool(
        name="mem_recall",
        description=(
            "Find relevant skills before starting a task. Searches skills by "
            "BM25 relevance weighted by Ebbinghaus strength — frequently used "
            "successful skills rank higher. Auto-reinforces retrieved skills."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Describe the task you're about to do."},
                "limit": {"type": "integer", "default": 5},
                "auto_reinforce": {
                    "type": "boolean", "default": True,
                    "description": "Bump strength of returned skills (Ebbinghaus reinforcement).",
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="mem_reinforce",
        description=(
            "Explicitly reinforce a skill after confirming it was useful. "
            "Bumps strength and access_count."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Skill slug to reinforce."},
            },
            "required": ["slug"],
        },
    ),
]


TOOL_HANDLERS = {
    "mem_search": _tool_search,
    "mem_get": _tool_get,
    "mem_list": _tool_list,
    "mem_write": _tool_write,
    "mem_update": _tool_update,
    "mem_learn": _tool_learn,
    "mem_recall": _tool_recall,
    "mem_reinforce": _tool_reinforce,
}


# --------------------------------------------------------------------------- #
# server wiring
# --------------------------------------------------------------------------- #


def _build_server() -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _err(f"unknown tool: {name}")
        return handler(arguments or {})

    return server


async def _async_main() -> None:
    server = _build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    run()
