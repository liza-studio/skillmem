"""HTTP layer: authentication, visibility and write permissions.

This file exists because two access-control bugs lived in server.py while the
storage core had 39 green tests: /recall leaked private skill bodies to every
authenticated agent (the visibility key it filtered on was never returned by
recall_skills, so `.get(..., "public")` always won), and /update gated writes on
read visibility alone. Both are covered below — keep them covered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

TOKENS_YAML = """\
boss:
  token: tok-boss
  scope: master
alice:
  token: tok-alice
  topics: [ops]
  permissions: [write_public]
bob:
  token: tok-bob
  topics: [ops]
carol:
  token: tok-carol
  topics: [finance]
"""


@pytest.fixture
def client(memhome: Path):
    """App wired to an isolated DB and a four-agent token file."""
    from skillmem import server as srv

    tokens = memhome / "tokens.yaml"
    tokens.write_text(TOKENS_YAML, encoding="utf-8")
    app = srv.build_app(srv.TokenStore(tokens), db_path=memhome / "memory.db")
    with TestClient(app) as c:
        yield c


def auth(agent: str) -> dict[str, str]:
    return {"Authorization": f"Bearer tok-{agent}"}


def write(client, agent, slug, *, visibility, kind="note", topics=(), body="body text"):
    r = client.post("/write", headers=auth(agent), json={
        "slug": slug, "title": slug, "body": body,
        "kind": kind, "visibility": visibility, "topics": list(topics),
    })
    return r


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #


def test_no_token_is_rejected(client):
    assert client.post("/search", json={"query": "x"}).status_code in (401, 403)


def test_bad_token_is_rejected(client):
    r = client.post("/search", headers={"Authorization": "Bearer nope"}, json={"query": "x"})
    assert r.status_code in (401, 403)


def test_whoami_reports_scope(client):
    r = client.post("/whoami", headers=auth("boss"))
    assert r.status_code == 200 and r.json()["scope"] == "master"


# --------------------------------------------------------------------------- #
# /recall — the leak
# --------------------------------------------------------------------------- #


def test_recall_hides_private_skills_of_other_agents(client):
    """REGRESSION: private skill bodies must not reach a different agent."""
    assert write(client, "alice", "skill-secret-deploy", visibility="private",
                 kind="skill", body="rotate the master key like so").status_code == 200

    r = client.post("/recall", headers=auth("bob"), json={"query": "deploy", "auto_reinforce": False})
    assert r.status_code == 200
    slugs = [s["slug"] for s in r.json()["skills"]]
    assert "skill-secret-deploy" not in slugs
    assert "master key" not in r.text


def test_recall_returns_own_private_skill(client):
    write(client, "alice", "skill-mine", visibility="private", kind="skill",
          body="alice private runbook")
    r = client.post("/recall", headers=auth("alice"), json={"query": "runbook", "auto_reinforce": False})
    assert "skill-mine" in [s["slug"] for s in r.json()["skills"]]


def test_recall_shared_requires_matching_topic(client):
    write(client, "alice", "skill-ops-thing", visibility="shared", kind="skill",
          topics=["ops"], body="shared ops runbook")

    assert "skill-ops-thing" in [
        s["slug"] for s in
        client.post("/recall", headers=auth("bob"),
                    json={"query": "runbook", "auto_reinforce": False}).json()["skills"]
    ]
    assert "skill-ops-thing" not in [
        s["slug"] for s in
        client.post("/recall", headers=auth("carol"),
                    json={"query": "runbook", "auto_reinforce": False}).json()["skills"]
    ]


def test_master_sees_everything_in_recall(client):
    write(client, "alice", "skill-secret", visibility="private", kind="skill", body="hidden runbook")
    r = client.post("/recall", headers=auth("boss"), json={"query": "runbook", "auto_reinforce": False})
    assert "skill-secret" in [s["slug"] for s in r.json()["skills"]]


def test_recall_does_not_reinforce_invisible_skills(client):
    """REGRESSION: auto_reinforce must not mutate records the caller cannot see."""
    from skillmem import storage as S

    write(client, "alice", "skill-untouchable", visibility="private", kind="skill",
          body="alice private runbook")
    conn = S.connect(Path(client.app.state.db_path) if hasattr(client.app, "state") and
                     getattr(client.app.state, "db_path", None) else _db_of(client))
    before = S.get(conn, "skill-untouchable").access_count

    client.post("/recall", headers=auth("bob"), json={"query": "runbook", "auto_reinforce": True})

    conn2 = S.connect(_db_of(client))
    assert S.get(conn2, "skill-untouchable").access_count == before


def _db_of(client) -> Path:
    """Locate the temp DB the app was built with (tokens.yaml sits beside it)."""
    import os
    return Path(os.environ["SKILLMEM_HOME"]) / "memory.db"


# --------------------------------------------------------------------------- #
# /update — write permissions
# --------------------------------------------------------------------------- #


def test_update_public_requires_write_public(client):
    """REGRESSION: seeing a public record is not permission to rewrite it."""
    assert write(client, "alice", "team-rule", visibility="public", body="original rule").status_code == 200

    denied = client.post("/update/team-rule", headers=auth("bob"),
                         json={"body": "bob's rewrite", "reason": "hijack"})
    assert denied.status_code == 403

    allowed = client.post("/update/team-rule", headers=auth("alice"),
                          json={"body": "alice's revision", "reason": "fix"})
    assert allowed.status_code == 200


def test_update_private_of_another_agent_is_not_found(client):
    write(client, "alice", "alice-note", visibility="private", body="private")
    r = client.post("/update/alice-note", headers=auth("bob"),
                    json={"body": "x", "reason": "y"})
    assert r.status_code == 404  # invisible records must not even confirm existence


def test_update_shared_by_non_author_is_forbidden(client):
    write(client, "alice", "ops-note", visibility="shared", topics=["ops"], body="v1")
    r = client.post("/update/ops-note", headers=auth("bob"),
                    json={"body": "v2", "reason": "edit"})
    assert r.status_code == 403  # bob can read it (same topic) but does not own it


def test_update_preserves_original_authorship(client):
    """An editor is not an author — master edits must not steal the byline."""
    from skillmem import storage as S

    write(client, "alice", "team-doc", visibility="public", body="v1")
    client.post("/update/team-doc", headers=auth("boss"), json={"body": "v2", "reason": "master edit"})

    conn = S.connect(_db_of(client))
    assert S.get(conn, "team-doc").agent == "alice"


def test_write_public_still_gated(client):
    """Baseline the /update fix was measured against."""
    assert write(client, "bob", "bob-public", visibility="public").status_code == 403
    assert write(client, "alice", "alice-public", visibility="public").status_code == 200


# --------------------------------------------------------------------------- #
# /search visibility (was already filtered — lock it in)
# --------------------------------------------------------------------------- #


def test_search_filters_private_of_others(client):
    write(client, "alice", "alice-secret", visibility="private", body="unique-marker-xyz")
    r = client.post("/search", headers=auth("bob"), json={"query": "unique-marker-xyz"})
    assert r.json()["count"] == 0
    assert "unique-marker-xyz" not in r.text


def test_search_200_with_embedding_present(client, memhome):
    """A row with a stored embedding must not break HTTP JSON serialization."""
    import sqlite3
    write(client, "boss", "emb-http", visibility="public", body="deploy nginx steps")
    db = sqlite3.connect(memhome / "memory.db")
    db.execute(
        "UPDATE memory_items SET embedding = ? WHERE slug = 'emb-http'",
        (b"\x00" * 1536,),
    )
    db.commit(); db.close()
    r = client.post("/search", headers=auth("boss"), json={"query": "deploy nginx"})
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert any(h["slug"] == "emb-http" for h in hits)
    assert all("embedding" not in h for h in hits)


def test_reinforce_gated_by_visibility(client):
    """No strength bumps (or existence probes) on records the agent can't see."""
    write(client, "carol", "carol-private-skill", visibility="private", kind="skill")
    r = client.post("/reinforce/carol-private-skill", headers=auth("bob"))
    assert r.status_code == 404
    r = client.post("/reinforce/carol-private-skill", headers=auth("carol"))
    assert r.status_code == 200
