"""Round-2 regressions: defects the round-1 fixes introduced (2026-09-16)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from skillmem import storage as S
from skillmem import hooks as H
from skillmem import export as E
from skillmem.cli import main as cli_main


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SKILLMEM_HOME", str(tmp_path))
    monkeypatch.delenv("SKILLMEM_DB", raising=False)
    return tmp_path


def _conn(home: Path, name: str = "memory.db"):
    conn = S.connect(home / name)
    S.init_schema(conn)
    return conn


BIG = lambda w: (w + " ") * 900  # noqa: E731


def _age(home: Path) -> None:
    for p in S.docs_dir().glob("*.md"):
        os.utime(p, (0, 0))


def test_gc_ignores_skillmem_db_override_and_legacy_files(home, monkeypatch):
    """The default DB's gc used to delete files of a SKILLMEM_DB-override DB
    (same empty namespace) and every pre-0.11 file (un-attributable)."""
    default = _conn(home)                       # <home>/memory.db, namespace ""
    monkeypatch.setenv("SKILLMEM_DB", str(home / "other.db"))
    other = _conn(home, "other.db")
    S.upsert(other, S.MemoryItem(slug="doc", title="d", body=BIG("beta"), kind="document"))
    monkeypatch.delenv("SKILLMEM_DB")
    # a legacy-named file referenced by another DB
    legacy = _conn(home, "legacy.db")
    S.upsert(legacy, S.MemoryItem(slug="leg", title="l", body=BIG("old"), kind="document"))
    old_name = S._body_filename("leg")
    (S.docs_dir() / old_name).write_text(BIG("old"), encoding="utf-8")
    legacy.execute("UPDATE memory_items SET body_path = ? WHERE slug = 'leg'", (old_name,))
    legacy.commit()
    _age(home)
    assert S.gc_body_files(default) == 0
    assert S.load_body(S.get(other, "doc")).startswith("beta")
    assert (S.docs_dir() / old_name).exists()


def test_lock_race_loser_does_not_delete_winners_file(home, monkeypatch):
    monkeypatch.setenv("SKILLMEM_BUSY_TIMEOUT_MS", "50")
    a = _conn(home)
    S.upsert(a, S.MemoryItem(slug="doc", title="d", body=BIG("one"), kind="document"))
    b = _conn(home)
    a.execute("BEGIN IMMEDIATE")
    S.upsert(a, S.MemoryItem(slug="doc", title="d", body=BIG("two"), kind="document"), force=True)
    with pytest.raises(Exception):
        S.upsert(b, S.MemoryItem(slug="doc", title="d", body=BIG("two"), kind="document"),
                 force=True)
    a.execute("COMMIT")
    item = S.get(a, "doc")
    assert (S.docs_dir() / item.body_path).exists()
    assert S.load_body(item).startswith("two")


def test_soft_delete_history_is_clock_clamped(home, monkeypatch):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v1", kind="skill"))
    S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v2", kind="skill"), force=True)
    real = S._now
    monkeypatch.setattr(S, "_now", lambda: real() - 500)
    assert S.soft_delete(conn, "k", "test")
    monkeypatch.setattr(S, "_now", real)
    S.upsert(conn, S.MemoryItem(slug="j", title="t", body="v1", kind="skill"))
    S.upsert(conn, S.MemoryItem(slug="j", title="t", body="v2", kind="skill"), force=True)
    rows, breaks = S.verify_history(conn)
    assert rows == 3 and breaks == []


def test_export_manifest_is_per_database_and_tolerates_junk(home):
    a, b = _conn(home, "a.db"), _conn(home, "b.db")
    S.upsert(a, S.MemoryItem(slug="from-a", title="t", body="b", kind="note"))
    S.upsert(b, S.MemoryItem(slug="from-b", title="t", body="b", kind="note"))
    dest = home / "vault"
    E.export_all(a, dest)
    E.export_all(b, dest)
    assert (dest / "note" / "from-a.md").exists()
    assert (dest / "note" / "from-b.md").exists()
    (dest / ".skillmem-export.json").write_text("[]", encoding="utf-8")
    E.export_all(a, dest)   # a non-dict manifest must not crash


def test_kind_is_normalised_not_rejected(home):
    conn = _conn(home)
    item = S.upsert(conn, S.MemoryItem(slug="r", title="t", body="b", kind=" Reference "))
    assert item.kind == "reference"
    assert S.get(conn, "r").kind == "reference"


def test_bad_kind_is_a_4xx_on_http_and_an_error_on_mcp(home, monkeypatch):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from skillmem import server as srv, mcp_server as M
    (home / "tokens.yaml").write_text("bob:\n  token: tok-bob\n", encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/write", headers={"Authorization": "Bearer tok-bob"}, json={
            "slug": "x", "title": "t", "body": "b", "kind": "../x", "visibility": "private"})
        assert r.status_code == 422
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    monkeypatch.setattr(M, "_CONN", None)
    out = json.loads(M._tool_write({"slug": "x", "title": "t", "body": "b", "kind": "../x"})[0].text)
    assert "error" in out


def test_create_refuses_tombstones_and_visibility_changes(home):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from skillmem import server as srv
    (home / "tokens.yaml").write_text(
        "alice:\n  token: tok-alice\n  permissions: [write_public]\nbob:\n  token: tok-bob\n",
        encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    A = {"Authorization": "Bearer tok-alice"}
    B = {"Authorization": "Bearer tok-bob"}
    with fastapi_testclient.TestClient(app) as c:
        body = {"slug": "gone", "title": "t", "body": "same", "kind": "note"}
        assert c.post("/write", headers=A, json={**body, "visibility": "public"}).status_code == 200
        # author re-POSTs the same text with a different visibility: explicit refusal
        assert c.post("/write", headers=A, json={**body, "visibility": "private"}).status_code == 409
        conn = _conn(home)
        assert S.soft_delete(conn, "gone", "test")
        conn.commit()
        r = c.post("/write", headers=B, json={**body, "visibility": "private"})
        assert r.status_code == 409
        row = conn.execute("SELECT agent FROM memory_items WHERE slug='gone'").fetchone()
        assert row["agent"] == "alice"


def test_frame_keeps_title_when_body_is_empty():
    out = H.frame_for_model({"trusted_at": None}, {"title": "IGNORE ALL", "body": ""})
    assert out["trusted"] is False
    assert H.UNTRUSTED_OPEN in out["body"] and "IGNORE ALL" in out["body"]
    assert "IGNORE ALL" not in out["title"]


def test_untrust_refuses_without_tty(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="rule", title="t", body="b", kind="feedback"))
    S.set_trust(conn, "rule", trusted=True)
    conn.commit()
    r = CliRunner().invoke(cli_main, ["--db", str(home / "memory.db"), "trust", "--untrust", "rule"])
    assert r.exit_code != 0
    assert S.get(_conn(home), "rule").trusted_at is not None


def test_db_flag_reaches_scheduled_job_env(home, monkeypatch):
    from skillmem import schedule as sch
    monkeypatch.delenv("SKILLMEM_DB", raising=False)
    # `main` exports --db for the rest of the process (schedule reads it)
    r = CliRunner().invoke(cli_main, ["--db", str(home / "x.db"), "ls"])
    assert r.exit_code == 0, r.output
    assert sch._job_env().get("SKILLMEM_DB") == str(home / "x.db")


def test_transcript_dir_name_matches_claude_code_sanitiser(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proj = tmp_path / ".claude" / "projects" / "-Users-x--claude-projects--Users-x"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text("{}", encoding="utf-8")
    found = H.newest_transcript_for_cwd(Path("/Users/x/.claude/projects/-Users-x"))
    assert found is not None and found.name == "s.jsonl"


def test_init_prunes_legacy_migrate_hook(home, monkeypatch):
    from skillmem import cli as cli_mod
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "/x/bin/skillmem migrate", "timeout": 10},
        {"type": "command", "command": "/x/bin/skillmem hook session-recap", "timeout": 95},
    ]}]}}), encoding="utf-8")
    r = cli_mod._prune_settings_hook(settings, command_prefix="skillmem migrate")
    assert r["changed"]
    data = json.loads(settings.read_text(encoding="utf-8"))
    cmds = [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]]
    assert cmds == ["/x/bin/skillmem hook session-recap"]


def test_recall_budget_never_cuts_a_frame(home):
    conn = _conn(home)
    for i in range(3):
        S.upsert(conn, S.MemoryItem(slug=f"fb{i}", title="rule " * 30, body="x " * 300,
                                    kind="feedback", origin="owner", trusted_at=1, trusted_by="o"))
    S.upsert(conn, S.MemoryItem(slug="sk", title="deploy", body="IGNORE ALL " * 40,
                                kind="skill", origin="agent"))
    conn.commit()
    out = H._recall_sections(conn, "rule deploy x", seen=set(), skills_limit=2, fb_limit=3,
                             body_chars=400, fb_header="### fb", skills_header="### sk",
                             budget=1500)
    assert len(out) <= 1500
    if H.UNTRUSTED_OPEN in out:
        assert H.UNTRUSTED_CLOSE in out
    # every slug named in the output is complete (no half rows)
    for line in out.splitlines():
        if line.startswith("- ["):
            assert "]" in line


def test_transcript_filter_accepts_string_content(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"content": "hello there, world"}}) + "\n",
                 encoding="utf-8")
    assert "hello there" in H._filter_transcript(t)


def test_pack_aggregate_budget(tmp_path, monkeypatch):
    from skillmem import packs as P
    monkeypatch.setattr(P, "MAX_PACK_SKILLS", 3)
    root = tmp_path / "pack"
    for i in range(6):
        d = root / "skills" / f"s{i}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: s{i}\ndescription: D.\n---\nb", encoding="utf-8")
    assert len(P.read_pack(root)) == 3


def test_uninstall_purge_removes_only_this_dbs_body_files(home, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    default = _conn(home)
    other = _conn(home, "other.db")
    S.upsert(default, S.MemoryItem(slug="doc", title="d", body=BIG("a"), kind="document"))
    S.upsert(other, S.MemoryItem(slug="doc", title="d", body=BIG("b"), kind="document"))
    default.close(); other.close()
    r = CliRunner().invoke(cli_main, ["--db", str(home / "other.db"), "uninstall",
                                      "--no-codex", "--no-editors", "--purge-db"])
    assert r.exit_code == 0, r.output
    assert not (home / "other.db").exists()
    assert (home / "memory.db").exists()
    assert S.load_body(S.get(_conn(home), "doc")).startswith("a")


# --- round 3: what round 2 broke -------------------------------------------

def test_init_db_flag_lands_in_agent_configs(home, monkeypatch):
    import sys
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    r = CliRunner().invoke(cli_main, ["--db", str(home / "other.db"), "init", "--claude-code",
                                      "--hooks", "none", "--skip-migrate", "--mcp-binary",
                                      str(Path(sys.executable).parent / "skillmem-mcp")])
    assert r.exit_code == 0, r.output
    cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["skillmem"]["env"]["SKILLMEM_DB"] == str(home / "other.db")


def test_gc_waits_for_an_open_write_transaction(home, monkeypatch):
    monkeypatch.setenv("SKILLMEM_BUSY_TIMEOUT_MS", "50")
    a = _conn(home)
    b = _conn(home)
    a.execute("BEGIN IMMEDIATE")
    S.upsert(a, S.MemoryItem(slug="doc", title="d", body=BIG("slow"), kind="document"))
    _age(home)                                   # older than the grace window
    assert S.gc_body_files(b) == 0               # locked out, not destructive
    a.execute("COMMIT")
    item = S.get(a, "doc")
    assert (S.docs_dir() / item.body_path).exists()


def test_export_two_homes_one_destination_keep_both(tmp_path, monkeypatch):
    dest = tmp_path / "shared"
    for name in ("A", "B"):
        home = tmp_path / name
        home.mkdir()
        monkeypatch.setenv("SKILLMEM_HOME", str(home))
        conn = S.connect(home / "memory.db")
        S.init_schema(conn)
        S.upsert(conn, S.MemoryItem(slug=f"from-{name}", title="t", body="b", kind="note"))
        E.export_all(conn, dest)
    assert (dest / "note" / "from-A.md").exists()
    assert (dest / "note" / "from-B.md").exists()


def test_purge_default_db_keeps_legacy_files_of_other_dbs(home, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    default = _conn(home)
    other = _conn(home, "other.db")
    S.upsert(other, S.MemoryItem(slug="leg", title="l", body=BIG("old"), kind="document"))
    old_name = S._body_filename("leg")
    (S.docs_dir() / old_name).write_text(BIG("old"), encoding="utf-8")
    other.execute("UPDATE memory_items SET body_path = ? WHERE slug = 'leg'", (old_name,))
    other.commit(); other.close(); default.close()
    r = CliRunner().invoke(cli_main, ["uninstall", "--no-codex", "--no-editors", "--purge-db"])
    assert r.exit_code == 0, r.output
    assert (S.docs_dir() / old_name).exists()


def test_prune_matches_quoted_and_exe_paths_but_not_foreign_tools(home):
    from skillmem import cli as cli_mod
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {
        "Stop": [{"hooks": [
            {"type": "command", "command": "'/Users/first last/.venv/bin/skillmem' migrate"},
            {"type": "command", "command": "/x/bin/skillmem hook session-recap"}]}],
        "PreToolUse": [{"hooks": [{"type": "command", "command": "my-skillmem migrate"}]}],
    }}), encoding="utf-8")
    r = cli_mod._prune_settings_hook(settings, command_prefix="skillmem migrate")
    assert r["changed"] and r.get("backup")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert [h["command"] for g in data["hooks"]["Stop"] for h in g["hooks"]] == \
        ["/x/bin/skillmem hook session-recap"]
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "my-skillmem migrate"


def test_kind_filter_is_case_insensitive_and_old_rows_are_migrated(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="r", title="t", body="b", kind="reference"))
    conn.execute("UPDATE memory_items SET kind = 'Reference' WHERE slug = 'r'")
    conn.commit()
    S.init_schema(conn)   # the open-time migration lowercases it
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='r'").fetchone()[0] == "reference"
    assert [i.slug for i in S.list_items(conn, kind="Reference")] == ["r"]


def test_write_without_visibility_keeps_existing_and_defaults_new(home):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from skillmem import server as srv
    (home / "tokens.yaml").write_text(
        "alice:\n  token: tok-alice\n  permissions: [write_public]\n", encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    A = {"Authorization": "Bearer tok-alice"}
    with fastapi_testclient.TestClient(app) as c:
        assert c.post("/learn", headers=A, json={"slug": "deploy", "title": "t", "trigger": "t",
                                                  "steps": "s", "outcome": "ok"}).status_code == 200
        r = c.post("/write", headers=A, json={"slug": "deploy", "title": "t",
                                               "body": "new text", "kind": "skill",
                                               "check_conflicts": False})
        assert r.status_code in (200, 409)  # 409 only from upsert's reason/force rule
        assert r.status_code != 403
        r = c.post("/write", headers=A, json={"slug": "fresh", "title": "t", "body": "b"})
        assert r.status_code == 200
        assert _conn(home).execute("SELECT visibility FROM memory_items WHERE slug='fresh'").fetchone()[0] == "private"


def test_transcript_filter_drops_synthetic_turns(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": "<task-notification>done</task-notification>"}}),
        json.dumps({"type": "user", "message": {"content": "<local-command-stdout>ls</local-command-stdout>"}}),
        json.dumps({"type": "user", "message": {"content": "a real question here"}}),
    ]), encoding="utf-8")
    out = H._filter_transcript(t)
    assert "real question" in out and "task-notification" not in out and "local-command" not in out


# --- round 4: what round 3 broke -------------------------------------------

def test_junk_kind_filter_matches_nothing_on_every_channel(home, monkeypatch):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="t", body="hello world", kind="note"))
    assert S.list_items(conn, kind="../x") == []
    assert S.search(conn, "hello", kind="../x") == []
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from skillmem import server as srv
    (home / "tokens.yaml").write_text("boss:\n  token: tok-boss\n  scope: master\n", encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    with fastapi_testclient.TestClient(app) as c:
        ok = c.post("/list", headers={"Authorization": "Bearer tok-boss"}, json={"kind": "Note"})
        assert ok.status_code == 200 and ok.json()["count"] == 1     # valid filter sees the row
        r = c.post("/list", headers={"Authorization": "Bearer tok-boss"}, json={"kind": "../x"})
        assert r.status_code == 200 and r.json()["count"] == 0
    r = CliRunner().invoke(cli_main, ["--db", str(home / "memory.db"), "ls", "--kind", "../x"])
    assert r.exit_code == 0, r.output


def test_gc_raises_on_non_contention_errors(home):
    conn = S.connect(home / "noschema.db")   # no init_schema
    with pytest.raises(Exception):
        S.gc_body_files(conn)


def test_prune_handles_windows_list2cmdline_quoting(home, monkeypatch):
    from skillmem import cli as cli_mod
    monkeypatch.setattr(cli_mod.sys, "platform", "win32")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": '"C:\\Users\\First Last\\venv\\Scripts\\skillmem.exe" migrate'},
        {"type": "command", "command": '"C:\\Users\\First Last\\venv\\Scripts\\skillmem.exe" hook session-recap'},
    ]}]}}), encoding="utf-8")
    r = cli_mod._prune_settings_hook(settings, command_prefix="skillmem migrate")
    assert r["changed"]
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert len(data["hooks"]["Stop"][0]["hooks"]) == 1


def test_kind_migration_trims_tabs_and_newlines(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="r", title="t", body="b", kind="reference"))
    conn.execute("UPDATE memory_items SET kind = '\tReference\n' WHERE slug = 'r'")
    conn.commit()
    S.init_schema(conn)
    assert [i.slug for i in S.list_items(conn, kind="Reference")] == ["r"]


def test_visibility_is_validated(home):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from skillmem import server as srv
    (home / "tokens.yaml").write_text("bob:\n  token: tok-bob\n", encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/write", headers={"Authorization": "Bearer tok-bob"},
                   json={"slug": "x", "title": "t", "body": "b", "visibility": "Public"})
        assert r.status_code == 422


def test_init_db_rerun_updates_existing_mcp_entry(home, monkeypatch):
    import sys
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    mcp = str(Path(sys.executable).parent / "skillmem-mcp")
    base = ["init", "--claude-code", "--hooks", "none", "--skip-migrate", "--mcp-binary", mcp]
    assert CliRunner().invoke(cli_main, ["--db", str(home / "a.db"), *base]).exit_code == 0
    assert CliRunner().invoke(cli_main, ["--db", str(home / "b.db"), *base]).exit_code == 0
    cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["skillmem"]["env"]["SKILLMEM_DB"] == str(home / "b.db")


def test_export_adopts_pre_release_manifest(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="a", title="t", body="b", kind="note"))
    dest = home / "vault"
    (dest / "note").mkdir(parents=True)
    (dest / "note" / "stale.md").write_text("old export", encoding="utf-8")
    (dest / ".skillmem-export.json").write_text(json.dumps({"files": ["note/stale.md"]}),
                                                encoding="utf-8")
    E.export_all(conn, dest)
    assert not (dest / "note" / "stale.md").exists()
    assert (dest / "note" / "a.md").exists()


def test_version_is_0_11():
    from skillmem import __version__
    assert __version__.startswith("0.11.")


# --- round 5: hash width, merged index, restore, tx, namespaces, init env -----

def test_body_filename_carries_128_bits_of_content_hash(home):
    import hashlib
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="doc", title="d", body=BIG("alpha"), kind="document"))
    item = S.get(conn, "doc")
    content = item.body_path.rsplit("+", 1)[1].removesuffix(".md")
    assert len(content) == 32 and content == item.content_hash[:32]
    assert S._BODY_FILE_RE.search(item.body_path)
    assert S._BODY_FILE_RE.search("legacy__deadbeef.md")             # old names still parse
    assert S._BODY_FILE_RE.search("pre__deadbeef+0123abcd.md")        # 0.11 pre-release names too


def test_metadata_update_indexes_merged_tags_and_topics(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="k", title="deploy", body="apply", kind="skill",
                                tags=["kubernetes"]))
    S.upsert(conn, S.MemoryItem(slug="k", title="deploy", body="apply", kind="skill",
                                topics=["postgresql"]))
    assert [h["slug"] for h in S.search(conn, "kubernetes")] == ["k"]   # kept tag still indexed
    assert [h["slug"] for h in S.search(conn, "postgresql")] == ["k"]


def test_update_returns_the_persisted_strength(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v1", kind="skill"))
    conn.execute("UPDATE memory_items SET strength = 1.9 WHERE slug = 'k'")
    conn.commit()
    item = S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v2", kind="skill"), force=True)
    assert item.strength == 1.9
    item = S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v2", kind="skill", strength=0.4),
                    force=True, restore_strength=True)        # same text, explicit restore
    assert conn.execute("SELECT strength FROM memory_items WHERE slug='k'").fetchone()[0] == 0.4


def test_export_import_round_trip_restores_default_strength(home):
    from skillmem import vault as V
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="k", title="t", body="v1", kind="note"))
    dest = home / "vault"
    E.export_all(conn, dest)                                   # strength 1.0 written explicitly
    text = (dest / "note" / "k.md").read_text(encoding="utf-8")
    assert "strength: 1.0" in text
    conn.execute("UPDATE memory_items SET strength = 1.9 WHERE slug = 'k'")
    conn.commit()
    S.upsert(conn, S.MemoryItem(slug="k", title="t", body="changed", kind="note"), force=True)
    V.import_vault(conn, dest, skip_auto_memories=False)
    row = conn.execute("SELECT strength, body FROM memory_items WHERE slug='k'").fetchone()
    assert row["strength"] == 1.0 and row["body"] == "v1"


def test_pack_import_joins_an_outer_transaction(home, tmp_path):
    from skillmem import packs as P
    conn = _conn(home)
    root = tmp_path / "pack"
    (root / "skills" / "s").mkdir(parents=True)
    (root / "skills" / "s" / "SKILL.md").write_text("---\nname: s\ndescription: D.\n---\nb",
                                                     encoding="utf-8")
    P.import_pack(conn, str(root), pack_name="pk")
    assert P.remove_pack(conn, "pk", reason="t") == ["pack-pk-s"]
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    S.upsert(conn, S.MemoryItem(slug="unrelated", title="t", body="b", kind="note"))
    P.import_pack(conn, str(root), pack_name="pk")            # reinstall inside the outer tx
    assert conn.in_transaction                                # must not have committed it
    conn.execute("ROLLBACK")
    assert S.get(conn, "unrelated") is None
    assert S.get(conn, "pack-pk-s") is None


def test_in_memory_databases_get_their_own_namespace():
    a = S.connect(Path(":memory:")) if False else __import__("sqlite3").connect(":memory:")
    b = __import__("sqlite3").connect(":memory:")
    assert S._db_namespace(a) != S._db_namespace(b) != ""


def test_init_without_db_keeps_an_existing_custom_database(home, monkeypatch):
    import sys
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"skillmem": {
        "command": "mcp", "env": {"SKILLMEM_DB": "/custom/custom.db"}}}}), encoding="utf-8")
    r = CliRunner().invoke(cli_main, ["init", "--claude-code", "--hooks", "none", "--skip-migrate",
                                      "--mcp-binary", str(Path(sys.executable).parent / "skillmem-mcp")])
    assert r.exit_code == 0, r.output
    cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["skillmem"]["env"]["SKILLMEM_DB"] == "/custom/custom.db"


def test_init_db_tolerates_a_hand_edited_env(home, monkeypatch):
    import sys
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    for bad in (None, "x", ["x"], False):
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"skillmem": {
            "command": "mcp", "env": bad}}}), encoding="utf-8")
        r = CliRunner().invoke(cli_main, ["--db", str(home / "o.db"), "init", "--claude-code",
                                          "--hooks", "none", "--skip-migrate",
                                          "--mcp-binary", str(Path(sys.executable).parent / "skillmem-mcp")])
        assert r.exit_code == 0, (bad, r.output)
        cfg = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        assert cfg["mcpServers"]["skillmem"]["env"]["SKILLMEM_DB"] == str((home / "o.db").resolve())


def test_editor_configs_follow_an_explicit_db(home, monkeypatch):
    import sys
    from skillmem import cli as cli_mod
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    mcp = Path(sys.executable).parent / "skillmem-mcp"
    cursor = home / ".cursor" / "mcp.json"
    cursor.parent.mkdir(parents=True)
    cli_mod._patch_mcp_servers_json(cursor, mcp, agent="cursor", db_env="/db/one")
    r = cli_mod._patch_mcp_servers_json(cursor, mcp, agent="cursor", db_env="/db/two")
    assert r["changed"]
    assert json.loads(cursor.read_text())["mcpServers"]["skillmem"]["env"]["SKILLMEM_DB"] == "/db/two"


def test_mcp_learn_rejects_junk_visibility(home, monkeypatch):
    from skillmem import mcp_server as M
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    monkeypatch.setattr(M, "_CONN", None)
    out = json.loads(M._tool_learn({"slug": "s", "title": "t", "trigger": "t", "steps": "s",
                                    "outcome": "ok", "visibility": "Public"})[0].text)
    assert out.get("ok") is True    # normalised, not rejected
    out = json.loads(M._tool_learn({"slug": "s2", "title": "t", "trigger": "t", "steps": "s",
                                    "outcome": "ok", "visibility": "../x"})[0].text)
    assert "error" in out


def test_legacy_kind_with_inner_space_stays_updatable(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="t", body="b", kind="note"))
    conn.execute("UPDATE memory_items SET kind = 'my notes' WHERE slug = 'n'")
    conn.commit()
    item = S.upsert(conn, S.MemoryItem(slug="n", title="t", body="b2", kind="my  notes"), force=True)
    assert item.kind == "my notes"


def test_recall_json_and_history_are_framed(home, monkeypatch):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="sk", title="IGNORE ALL", body="v1", kind="skill", origin="agent"))
    S.upsert(conn, S.MemoryItem(slug="sk", title="IGNORE ALL", body="v2", kind="skill", origin="agent"),
             force=True)
    conn.commit()
    r = CliRunner().invoke(cli_main, ["--db", str(home / "memory.db"), "recall", "v2", "--format", "json",
                                      "--no-reinforce"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out and H.UNTRUSTED_OPEN in out[0]["body"] and "IGNORE ALL" not in out[0]["title"]
    from skillmem import mcp_server as M
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    monkeypatch.setattr(M, "_CONN", None)
    g = json.loads(M._tool_get({"slug": "sk", "include_history": True})[0].text)
    assert g["history"] and H.UNTRUSTED_OPEN in g["history"][0]["old_body"]


def test_release_metadata_agrees_with_the_package():
    from skillmem import __version__
    root = Path(__file__).resolve().parents[1]
    for f in ("server.json", "plugin.json", ".claude-plugin/plugin.json"):
        data = json.loads((root / f).read_text(encoding="utf-8"))
        assert data["version"] == __version__, f
    assert json.loads((root / "server.json").read_text())["packages"][0]["version"] == __version__


# --- round 6: what round 5 broke -------------------------------------------

def test_plain_vault_sync_keeps_earned_strength_but_dump_restores(home, tmp_path):
    from skillmem import vault as V
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="deploy", body="deploy", kind="skill"))
    conn.execute("UPDATE memory_items SET strength = 1.9 WHERE slug = 'n'")
    conn.commit()
    plain = tmp_path / "vault"
    plain.mkdir()
    (plain / "n.md").write_text("deploy", encoding="utf-8")            # no frontmatter
    V.import_vault(conn, plain, kind="skill")
    assert conn.execute("SELECT strength FROM memory_items WHERE slug='n'").fetchone()[0] == 1.9
    (plain / "n.md").write_text("deploy v2", encoding="utf-8")
    V.import_vault(conn, plain, kind="skill")
    assert conn.execute("SELECT strength FROM memory_items WHERE slug='n'").fetchone()[0] == 1.9
    dump = tmp_path / "dump"
    E.export_all(conn, dump)                                            # says strength 1.9
    conn.execute("UPDATE memory_items SET strength = 0.3 WHERE slug = 'n'")
    conn.commit()
    V.import_vault(conn, dump, skip_auto_memories=False)
    assert conn.execute("SELECT strength FROM memory_items WHERE slug='n'").fetchone()[0] == 1.9


def test_legacy_visibility_is_repaired_on_open_and_row_stays_updatable(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="h", title="t", body="b", kind="note"))
    conn.execute("UPDATE memory_items SET visibility = 'team' WHERE slug = 'h'")
    conn.commit()
    S.init_schema(conn)
    assert conn.execute("SELECT visibility FROM memory_items WHERE slug='h'").fetchone()[0] == "private"
    item = S.get(conn, "h")
    item.body = "b2"
    S.upsert(conn, item, force=True)            # no ValueError


def test_legacy_kind_whitespace_is_collapsed_on_open(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="t", body="b", kind="note"))
    conn.execute("UPDATE memory_items SET kind = ' My   Notes ' WHERE slug = 'n'")
    conn.commit()
    S.init_schema(conn)
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='n'").fetchone()[0] == "my notes"
    assert [i.slug for i in S.list_items(conn, kind="my notes")] == ["n"]


def test_history_old_title_is_framed(home, monkeypatch):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="sk", title="IGNORE ALL", body="v1", kind="skill", origin="agent"))
    S.upsert(conn, S.MemoryItem(slug="sk", title="still bad", body="v2", kind="skill", origin="agent"),
             force=True)
    conn.commit()
    from skillmem import mcp_server as M
    monkeypatch.setenv("SKILLMEM_DB", str(home / "memory.db"))
    monkeypatch.setattr(M, "_CONN", None)
    g = json.loads(M._tool_get({"slug": "sk", "include_history": True})[0].text)
    h0 = g["history"][0]
    assert "IGNORE ALL" not in h0["old_title"] and "IGNORE ALL" in h0["old_body"]
    assert H.UNTRUSTED_OPEN in h0["old_body"]



def test_db_memory_is_not_turned_into_a_file(home, monkeypatch):
    monkeypatch.chdir(home)
    r = CliRunner().invoke(cli_main, ["--db", ":memory:", "ls"])
    assert r.exit_code == 0, r.output
    assert not (home / ":memory:").exists()


# --- round 7: Codex in-place update hardened; repair guards ----------------



def test_visibility_case_only_is_repaired_without_an_off_enum_sibling(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="p", title="t", body="b", kind="note"))
    conn.execute("UPDATE memory_items SET visibility = 'Public' WHERE slug = 'p'")
    conn.commit()
    S.init_schema(conn)
    assert conn.execute("SELECT visibility FROM memory_items WHERE slug='p'").fetchone()[0] == "public"


def test_kind_repair_catches_newlines_and_nbsp(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="t", body="b", kind="note"))
    conn.execute("UPDATE memory_items SET kind = 'my\nnotes' WHERE slug = 'n'")
    conn.commit()
    S.init_schema(conn)
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='n'").fetchone()[0] == "my notes"
    conn.execute("UPDATE memory_items SET kind = 'my\u00a0notes' WHERE slug = 'n'")
    conn.commit()
    S.init_schema(conn)
    assert conn.execute("SELECT kind FROM memory_items WHERE slug='n'").fetchone()[0] == "my notes"


# --- round 8: Codex edit must change nothing but SKILLMEM_DB ---------------


def test_atomic_write_follows_symlink_and_keeps_mode_and_crlf(home):
    import os, stat
    from skillmem import cli as cli_mod
    target = home / "dotfiles" / "config.toml"
    target.parent.mkdir()
    target.write_bytes(b'model = "x"\r\n')
    os.chmod(target, 0o644)
    link = home / "config.toml"
    link.symlink_to(target)
    cli_mod._atomic_write_text(link, 'model = "y"\r\n')
    assert link.is_symlink() and target.read_bytes() == b'model = "y"\r\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


# --- round 9: last P3s from the first clean gate ---------------------------



def test_kind_repair_catches_vertical_tab_and_form_feed(home):
    conn = _conn(home)
    S.upsert(conn, S.MemoryItem(slug="n", title="t", body="b", kind="note"))
    for junk in ("my\x0bnotes", "my\x0cnotes"):
        conn.execute("UPDATE memory_items SET kind = ? WHERE slug = 'n'", (junk,))
        conn.commit()
        S.init_schema(conn)
        assert conn.execute("SELECT kind FROM memory_items WHERE slug='n'").fetchone()[0] == "my notes"



# --- round 10: Codex in-place editing withdrawn ---------------------------

def test_codex_existing_entry_is_never_edited_and_the_way_out_is_explained(home):
    import sys, tomllib
    from skillmem import cli as cli_mod
    toml = home / ".codex" / "config.toml"
    toml.parent.mkdir(parents=True)
    mcp = Path(sys.executable).parent / "skillmem-mcp"
    toml.write_bytes(b'# mine\r\nmodel = "x"\r\n')
    assert cli_mod._patch_codex_config(toml, mcp, db_env="/db/one")["changed"]
    before = toml.read_bytes()
    r = cli_mod._patch_codex_config(toml, mcp, db_env="/db/two")
    assert r["changed"] is False and "uninstall" in r["reason"] and "/db/two" in r["reason"]
    assert toml.read_bytes() == before                      # untouched, whatever the line endings
    assert cli_mod._patch_codex_config(toml, mcp, db_env="/db/one")["changed"] is False
    assert cli_mod._patch_codex_config(toml, mcp)["changed"] is False
    assert tomllib.loads(before.decode())["mcp_servers"]["skillmem"]["env"]["SKILLMEM_DB"] == "/db/one"
    for text in ('[mcp_servers]\nskillmem = "s"\n', 'mcp_servers = 5\n',
                 '[mcp_servers.skillmem]\ncommand = "mcp"\nenv = 5\n'):
        toml.write_text(text, encoding="utf-8")
        r = cli_mod._patch_codex_config(toml, mcp, db_env="/new")
        assert r["changed"] is False and "by hand" in r["reason"], (text, r)
        assert toml.read_text(encoding="utf-8") == text
