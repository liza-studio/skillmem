"""INV-12: the hooks `init --claude-code` installs use the database its MCP
entry uses. INV-08: `migrate` that imported nothing does not exit 0."""
import json
import shlex
import sys

import pytest
from click.testing import CliRunner

from skillmem import cli, storage as S
from .support import database


def _argv(command):
    """A hook command as its platform's quoting wrote it: list2cmdline on
    Windows (cli._hook_cmd), where POSIX shlex took each backslash as an escape."""
    if sys.platform != "win32":
        return shlex.split(command)
    return [a.strip('"') for a in shlex.split(command, posix=False)]


def _database_of(argv, ambient):
    """The database a skillmem command line opens under ``ambient`` env."""
    ctx = cli.main.make_context("skillmem", list(argv[1:]), resilient_parsing=True)
    return str(ctx.params["db_path"]) if ctx.params["db_path"] else ambient


@pytest.mark.parametrize("runs", [["a"], [None], ["a", None], ["a", "b"], [None, "a"]])
def test_every_hook_opens_the_database_the_mcp_server_opens(runs):
    """INV-12 (*r15 opus review*): `--db X init --claude-code` pointed the MCP
    server at X and every hook at the default database, so recaps were
    written where no recall read them and X's rules never reached a session."""
    with database() as (conn, root, patch):
        patch.setattr(cli, "_mcp_bin", lambda given: root / "skillmem-mcp")
        for name in runs:
            args = (["--db", str(root / f"{name}.db")] if name else []) + [
                "init", "--claude-code"]
            patch.setenv("SKILLMEM_DB", str(root / "memory.db"))   # main() rewrites it
            result = CliRunner().invoke(cli.main, args)
            assert result.exit_code == 0, result.output
        ambient = str(root / "memory.db")   # what Claude Code's environment says
        mcp = json.loads((root / ".claude.json").read_text())["mcpServers"]["skillmem"]
        mcp_db = (mcp.get("env") or {}).get("SKILLMEM_DB", ambient)
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        commands = [h["command"] for groups in settings["hooks"].values()
                    for g in groups for h in g["hooks"]]
        assert len(commands) == 9
        for command in commands:
            assert _database_of(_argv(command), ambient) == mcp_db, command
        # a second init repoints, never doubles
        result = CliRunner().invoke(cli.main, ["init", "--claude-code"])
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        assert sum(len(g["hooks"]) for gs in settings["hooks"].values() for g in gs) == 9
        result = CliRunner().invoke(cli.main, ["uninstall"], input="y\n")
        settings = json.loads((root / ".claude" / "settings.json").read_text())
        assert not settings.get("hooks"), settings


def test_migrate_that_imports_nothing_without_a_terminal_fails():
    """INV-08 (*r15 opus review*): with no terminal and no --source, `migrate`
    imported nothing and exited 0 with a warning, which is not a reported
    failure."""
    with database() as (conn, root, patch):
        mem = root / ".claude" / "projects" / "p1" / "memory"
        mem.mkdir(parents=True)
        (mem / "feedback_r1.md").write_text("---\nname: r1\ntype: feedback\n---\nbody")
        result = CliRunner().invoke(cli.main, ["migrate"])
        assert result.exit_code != 0, result.output
        assert S.list_items(conn) == []
