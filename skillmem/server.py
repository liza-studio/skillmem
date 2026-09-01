"""HTTP API for shared multi-agent access.

Mirrors the MCP tools as REST endpoints with bearer-token auth and visibility
scoping. Bound to 127.0.0.1 by default; expose with care.

Token file format (YAML)::

    admin:                        # master — sees everything
      token: <random>
      scope: master
    researcher:
      token: <random>
      permissions: [write_public]               # may write/edit public rows
      topics: [research, docs]                  # 'shared' rows must match
    analyst:
      token: <random>
      topics: [research, metrics]

Visibility rules:
- ``public`` — everyone.
- ``shared`` — only agents whose ``topics`` intersect the row's topics.
- ``private`` — only the author (``agent`` column).
- ``master`` scope — bypasses all of the above.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import storage as S


# --------------------------------------------------------------------------- #
# token loading
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    token: str
    scope: str = "agent"  # 'master' | 'agent'
    topics: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()  # e.g. {'write_public'}

    @property
    def is_master(self) -> bool:
        return self.scope == "master"

    def can(self, permission: str) -> bool:
        return self.is_master or permission in self.permissions


class TokenStore:
    """In-memory bearer-token → agent lookup, reloadable on SIGHUP."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_token: dict[str, AgentIdentity] = {}
        self.reload()

    def reload(self) -> None:
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("tokens file must be a YAML mapping of agent → config")
        bucket: dict[str, AgentIdentity] = {}
        for name, cfg in raw.items():
            if not isinstance(cfg, dict) or "token" not in cfg:
                raise ValueError(f"agent '{name}' missing 'token'")
            scope = cfg.get("scope", "agent")
            topics = tuple(cfg.get("topics", []) or [])
            perms = frozenset(cfg.get("permissions", []) or [])
            ident = AgentIdentity(
                name=name, token=cfg["token"],
                scope=scope, topics=topics, permissions=perms,
            )
            if ident.token in bucket:
                raise ValueError(f"duplicate token for agents {bucket[ident.token].name} and {name}")
            bucket[ident.token] = ident
        self._by_token = bucket

    def resolve(self, token: str) -> AgentIdentity | None:
        return self._by_token.get(token)


# --------------------------------------------------------------------------- #
# visibility filter — applied to every result row in the HTTP layer
# --------------------------------------------------------------------------- #


def _visible_to(row: dict[str, Any] | S.MemoryItem, agent: AgentIdentity) -> bool:
    if agent.is_master:
        return True
    if isinstance(row, S.MemoryItem):
        visibility = row.visibility
        topics = row.topics
        author = row.agent
    else:
        visibility = row.get("visibility", "private")
        topics = row.get("topics") or []
        author = row.get("agent")
    if visibility == "public":
        return True
    if visibility == "shared":
        return any(t in agent.topics for t in topics)
    if visibility == "private":
        return author == agent.name
    return False


def _may_write(row: dict[str, Any] | S.MemoryItem, agent: AgentIdentity) -> bool:
    """Write permission for an EXISTING record — stricter than _visible_to.

    Read access answers "may I see this"; this answers "may I overwrite it".
    Public records are team-wide, so they need the same 'write_public'
    permission /write demands; everything else belongs to its author.
    """
    if agent.is_master:
        return True
    if isinstance(row, S.MemoryItem):
        visibility, author = row.visibility, row.agent
    else:
        visibility, author = row.get("visibility", "private"), row.get("agent")
    if visibility == "public":
        return agent.can("write_public")
    return author == agent.name


# --------------------------------------------------------------------------- #
# request / response shapes
# --------------------------------------------------------------------------- #


class SearchRequest(BaseModel):
    query: str
    kind: str | None = None
    project: str | None = None
    limit: int = Field(10, ge=1, le=100)


class WriteRequest(BaseModel):
    slug: str
    title: str
    body: str
    kind: str = "note"
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    visibility: str = "private"
    ttl_days: int | None = None
    check_conflicts: bool = True


class UpdateRequest(BaseModel):
    body: str
    reason: str
    title: str | None = None
    kind: str | None = None
    project: str | None = None
    topics: list[str] | None = None
    tags: list[str] | None = None


class ListRequest(BaseModel):
    kind: str | None = None
    project: str | None = None
    limit: int = Field(50, ge=1, le=500)


class LearnRequest(BaseModel):
    slug: str
    title: str
    trigger: str
    steps: str
    outcome: str
    lessons: str | None = None
    project: str | None = None
    tags: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    visibility: str = "public"
    ttl_days: int | None = None
    check_conflicts: bool = True


class RecallRequest(BaseModel):
    query: str
    limit: int = Field(5, ge=1, le=50)
    auto_reinforce: bool = True


# --------------------------------------------------------------------------- #
# server build
# --------------------------------------------------------------------------- #


def build_app(token_store: TokenStore, db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="skillmem", version="0.1.0")
    bearer = HTTPBearer(auto_error=True)

    # One connection per worker thread, reused across requests. The previous
    # code opened a fresh connection on every call and never closed it: each
    # request left a WAL reader alive until the GC happened to collect it.
    # SQLite connections are not safe to share between threads, so a
    # thread-local is the bounded fix — at most one per threadpool worker.
    _conns = threading.local()

    def get_conn() -> sqlite3.Connection:
        conn = getattr(_conns, "conn", None)
        if conn is None:
            conn = S.connect(db_path)
            S.init_schema(conn)
            _conns.conn = conn
        return conn

    def get_agent(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> AgentIdentity:
        ident = token_store.resolve(credentials.credentials)
        if ident is None:
            raise HTTPException(status_code=401, detail="invalid bearer token")
        return ident

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": "0.1.0"}

    @app.post("/whoami")
    def whoami(agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        return {"agent": agent.name, "scope": agent.scope, "topics": list(agent.topics)}

    @app.post("/search")
    def search(req: SearchRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        hits = S.search(conn, req.query, kind=req.kind, project=req.project, limit=req.limit)
        filtered = [h for h in hits if _visible_to(h, agent)]
        return {"count": len(filtered), "results": filtered, "agent": agent.name}

    @app.get("/get/{slug}")
    def get_one(slug: str, include_history: bool = False,
                agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        item = S.get(conn, slug)
        if not item or not _visible_to(item, agent):
            raise HTTPException(status_code=404, detail="not found")
        payload = item.to_dict()
        payload["body"] = S.load_body(item)
        payload["links_out"] = S.links_from(conn, slug)
        payload["links_in"] = S.links_to(conn, slug)
        if include_history:
            payload["history"] = S.history(conn, slug)
        return payload

    @app.post("/list")
    def list_(req: ListRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        items = S.list_items(conn, kind=req.kind, project=req.project, limit=req.limit * 2)
        visible = [i for i in items if _visible_to(i, agent)][: req.limit]
        return {
            "count": len(visible),
            "items": [
                {"slug": i.slug, "kind": i.kind, "title": i.title,
                 "project": i.project, "updated_at": i.updated_at,
                 "visibility": i.visibility}
                for i in visible
            ],
        }

    @app.post("/write")
    def write(req: WriteRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        if req.visibility == "public" and not agent.can("write_public"):
            raise HTTPException(
                status_code=403,
                detail="agent lacks 'write_public' permission (or master scope)",
            )
        conn = get_conn()
        item = S.MemoryItem(
            slug=req.slug, kind=req.kind, title=req.title, body=req.body,
            project=req.project, tags=list(req.tags), topics=list(req.topics),
            visibility=req.visibility, agent=agent.name, ttl_days=req.ttl_days,
        )
        try:
            result = S.upsert(
                conn, item,
                check_conflicts=req.check_conflicts,
                links=S.extract_wikilinks(req.body),
            )
        except S.MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "slug": result.slug, "id": result.id, "agent": agent.name}

    @app.post("/update/{slug}")
    def update(slug: str, req: UpdateRequest,
               agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        existing = S.get(conn, slug)
        if not existing or not _visible_to(existing, agent):
            raise HTTPException(status_code=404, detail="not found")
        # Seeing a record is not permission to rewrite it. /write already gates
        # public writes behind 'write_public'; /update used to gate on read
        # visibility alone, so any agent could rewrite shared team memory and
        # take over its authorship.
        if not _may_write(existing, agent):
            raise HTTPException(
                status_code=403,
                detail=(
                    "agent lacks permission to update this record "
                    "(public requires 'write_public'; otherwise only the author or master)"
                ),
            )
        original_author = existing.agent
        existing.body = req.body
        if req.title is not None:
            existing.title = req.title
        if req.kind is not None:
            existing.kind = req.kind
        if req.project is not None:
            existing.project = req.project
        if req.topics is not None:
            existing.topics = list(req.topics)
        if req.tags is not None:
            existing.tags = list(req.tags)
        # Authorship stays with whoever created the record; an editor is not an
        # author. Only fill it in when the record never had one.
        existing.agent = original_author or agent.name
        try:
            result = S.upsert(
                conn, existing, reason=req.reason,
                links=S.extract_wikilinks(req.body),
            )
        except S.MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "slug": result.slug}

    @app.post("/learn")
    def learn(req: LearnRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        item = S.MemoryItem(
            slug=req.slug, kind="skill", title=req.title,
            body=S.skill_body(req.trigger, req.steps, req.outcome, req.lessons),
            project=req.project,
            tags=list(req.tags), topics=list(req.topics),
            visibility=req.visibility, agent=agent.name,
            ttl_days=req.ttl_days,
        )
        try:
            result = S.upsert(
                conn, item,
                check_conflicts=req.check_conflicts,
                links=S.extract_wikilinks(item.body),
            )
        except S.MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"ok": True, "slug": result.slug, "id": result.id, "kind": "skill", "agent": agent.name}

    @app.post("/recall")
    def recall(req: RecallRequest, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        # Reinforce AFTER the visibility filter: bumping strength on a skill the
        # caller may not see is both a side effect they should not be able to
        # trigger and a covert channel into someone else's memory.
        results = S.recall_skills(conn, req.query, limit=req.limit, auto_reinforce=False)
        visible = [r for r in results if _visible_to(r, agent)]
        if req.auto_reinforce:
            for r in visible:
                bumped = S.reinforce(conn, r["slug"])
                if bumped:
                    r["strength"] = bumped["strength"]
                    r["access_count"] = bumped["access_count"]
        return {"count": len(visible), "skills": visible, "agent": agent.name}

    @app.post("/reinforce/{slug}")
    def reinforce(slug: str, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        conn = get_conn()
        # Visibility gate: without it any agent could bump strength of skills
        # it cannot see — a write side-channel into someone else's memory, and
        # a slug-existence oracle (200 vs 404). Same 404 for both cases.
        item = S.get(conn, slug)
        if not item or not _visible_to(item, agent):
            raise HTTPException(status_code=404, detail="not found")
        result = S.reinforce(conn, slug)
        if not result:
            raise HTTPException(status_code=404, detail="not found")
        return result

    @app.post("/decay")
    def decay(days: int = 14, agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        if not agent.is_master:
            raise HTTPException(status_code=403, detail="master scope required")
        conn = get_conn()
        decayed = S.decay_stale(conn, days_threshold=days)
        return {"decayed": len(decayed), "details": decayed}

    @app.post("/reload-tokens")
    def reload_tokens(agent: AgentIdentity = Depends(get_agent)) -> dict[str, Any]:
        if not agent.is_master:
            raise HTTPException(status_code=403, detail="master scope required")
        token_store.reload()
        return {"ok": True}

    return app


# --------------------------------------------------------------------------- #
# entry point (`skillmem-server`)
# --------------------------------------------------------------------------- #


def _default_tokens_path() -> Path:
    return S.default_data_dir() / "agent_tokens.yaml"


def run() -> None:
    """CLI entry: skillmem-server [--host 127.0.0.1] [--port 7000] [--tokens PATH]"""
    import argparse

    parser = argparse.ArgumentParser(prog="skillmem-server")
    parser.add_argument("--host", default=os.environ.get("SKILLMEM_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SKILLMEM_PORT", "7000")))
    parser.add_argument("--tokens", type=Path,
                        default=Path(os.environ.get("SKILLMEM_TOKENS", _default_tokens_path())))
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    if not args.tokens.exists():
        raise SystemExit(
            f"tokens file not found: {args.tokens}\n"
            f"create it via: skillmem tokens-init {args.tokens}"
        )

    store = TokenStore(args.tokens)
    app = build_app(store, db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    run()
