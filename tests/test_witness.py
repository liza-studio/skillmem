"""`test_passed` over MCP counts only when the harness saw a test pass."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest
from click.testing import CliRunner

from skillmem import cli, storage as S


@pytest.fixture
def mcp(memhome):
    from skillmem import mcp_server as m
    m._CONN = None
    m.TOOL_HANDLERS["mem_learn"]({"slug": "skill-x", "title": "x", "trigger": "t",
                                  "steps": "s", "outcome": "success", "lessons": "l"})
    yield m
    m._CONN.close()
    m._CONN = None


def _reinforce(mcp) -> dict:
    return json.loads(mcp.TOOL_HANDLERS["mem_reinforce"](
        {"slug": "skill-x", "evidence": "test_passed"})[0].text)


def _bash(command: str, **fields) -> dict:
    return {"hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False}, **fields}


def _hook(payload: dict) -> None:
    result = CliRunner().invoke(cli.main, ["hook", "test-witness"], input=json.dumps(payload))
    assert result.exit_code == 0 and not result.output


def test_forged_test_passed_does_not_move_strength(mcp):
    result = _reinforce(mcp)
    assert result["strength"] == 1.0 and result["confirmed_count"] == 0
    assert result["evidence"] == "self_report" and "warning" in result


def test_witnessed_test_passed_moves_strength(mcp):
    _hook(_bash("python -m pytest -q"))
    result = _reinforce(mcp)
    assert result["strength"] > 1.0 and result["confirmed_count"] == 1
    assert result["evidence"] == "test_passed" and "warning" not in result


@pytest.mark.parametrize("offset", [-S.WITNESS_TTL_SECONDS - 60, 3600], ids=["expired", "future"])
def test_a_witness_out_of_its_window_does_not_count(mcp, offset):
    _hook(_bash("pytest"))
    then = time.time() + offset
    os.utime(S.witness_path(), (then, then))
    result = _reinforce(mcp)
    assert result["strength"] == 1.0 and result["evidence"] == "self_report"


def test_cli_and_library_callers_are_not_gated(conn):
    S.upsert(conn, S.MemoryItem(slug="sk", kind="skill", title="t", body="b"), surface="cli")
    assert S.reinforce(conn, "sk", evidence="test_passed", surface="cli")["strength"] > 1.0


def test_no_mcp_tool_can_write_the_witness(mcp):
    name = S.witness_path().name
    for slug in (name, f"../{name}", str(S.witness_path())):
        mcp.TOOL_HANDLERS["mem_write"]({"slug": slug, "title": "t", "body": "b"})
    assert not S.witnessed_pass()
    assert not any(word in prop for tool in mcp.TOOLS
                   for prop in tool.inputSchema.get("properties", {})
                   for word in ("path", "file", "witness"))


def test_a_witness_backs_only_its_own_database(memhome, tmp_path):
    mine, other = tmp_path / "mine.db", tmp_path / "other.db"
    result = CliRunner().invoke(cli.main, ["--db", str(mine), "hook", "test-witness"],
                                input=json.dumps(_bash("pytest")))
    assert result.exit_code == 0
    assert S.witnessed_pass(mine) and not S.witnessed_pass(other)


@pytest.mark.parametrize("command", [
    "pytest", "py.test -q", "python3 -m pytest tests/ -k x", ".venv/bin/python -m unittest",
    "cd sub/dir && .venv/bin/pytest -q", "pytest -q 2>&1", "npm test", "npm run test",
    "go test ./...", "cargo test", "cargo nextest run", "make test", "./gradlew test",
    "uv run pytest", "npx vitest run",
])
def test_a_lone_test_runner_is_witnessed(memhome, command):
    _hook(_bash(command))
    assert S.witnessed_pass()


@pytest.mark.parametrize("command", [
    "pytest | tail -5", "pytest || true", "pytest; echo done", "pytest && true", "true && pytest",
    "pytest &", "pytest > out.txt", "pytest $(x)", "pytest `x`", "pytest\ntrue",
    "echo pytest", "grep pytest setup.cfg", "sh -c pytest", "a;b/pytest", "git log --grep test",
    "pytest --collect-only", "pytest --co", "pytest --help", "cargo test --no-run", "go test -list .",
    "", "   ",
])
def test_anything_else_is_not(memhome, command):
    _hook(_bash(command))
    assert not S.witnessed_pass()


@pytest.mark.parametrize("change", [
    {"hook_event_name": "PostToolUseFailure"},
    {"tool_name": "PowerShell"},
    {"tool_input": {"command": "pytest", "run_in_background": True}},
    {"tool_response": {"interrupted": True}},
    {"tool_response": {"backgroundTaskId": "b1"}},
    {"tool_response": {"timedOutAfterMs": 1}},
    {"tool_response": {"returnCodeInterpretation": "No matches found"}},
    {"tool_response": "ok"},
    {"tool_input": "pytest"},
    {"tool_input": {"command": ["pytest"]}},
], ids=str)
def test_a_result_that_is_not_a_clean_exit_is_not_witnessed(memhome, change):
    _hook(_bash("pytest", **change))
    assert not S.witnessed_pass()


@pytest.mark.parametrize("stdin", [b"", b"\xff\xfe", b"[]", b"{", b'{"tool_response": 1}', b"null"])
def test_hook_on_malformed_payload_exits_zero(memhome, stdin):
    result = subprocess.run([sys.executable, "-P", "-m", "skillmem.cli", "hook", "test-witness"],
                            input=stdin, capture_output=True, timeout=30, env=os.environ.copy())
    assert result.returncode == 0 and b"Traceback" not in result.stderr
    assert not S.witnessed_pass()
