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
    (home / "tokens.yaml").write_text("bob:\n  token: tok-bob\n", encoding="utf-8")
    app = srv.build_app(srv.TokenStore(home / "tokens.yaml"), db_path=home / "memory.db")
    with fastapi_testclient.TestClient(app) as c:
        r = c.post("/list", headers={"Authorization": "Bearer tok-bob"}, json={"kind": "../x"})
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
