# skillmem

Memory layer for a team of AI agents. SQLite + FTS5 + MCP. Local-first, no API costs.


## Install (dev)

```bash
uv pip install -e .
skillmem doctor
```

## CLI

```bash
skillmem init                    # create DB
skillmem migrate                 # import ~/.claude/projects/-Users-macrazrab/memory/*.md
skillmem search "галлюцинации"
skillmem cat feedback-no-hallucinations [--history] [--links]
skillmem ls --kind feedback
skillmem write --slug new-rule --title "..." --body-file body.md --kind feedback \
               --ttl-days 30                 # add TTL → search marks as [stale] after
skillmem write --slug ... --no-check-conflicts # bypass duplicate detection
skillmem rm <slug> --reason "..."              # (updates go through `write` + reason or MCP mem_update)
skillmem verify [--strict]                     # SHA256 hash-chain over memory_history (tamper-evidence)
skillmem inject --types user,feedback --budget 1500    # SessionStart briefing
skillmem export-all /tmp/skillmem-dump       # round-trip-safe .md dump
skillmem import-vault ~/Obsidian/DC_GROUP    # bulk import (kind=document)
skillmem doctor                              # stats + schema version
```

### Pillars implemented in MVP

- **Birth/expiration/death record** — every row has `created_at`, optional
  `freshness_until` (via `--ttl-days`), and old versions land in
  `memory_history` on update.
- **Conflict detection** — `mem_write` refuses near-dupes (Jaccard-style
  inclusion overlap > 0.7). Bypass with `--no-check-conflicts`.
- **Vendor-lock defense** — `export-all` dumps every memory back to .md with
  YAML frontmatter, lossless round-trip with `import-vault`.

## MCP

Run as MCP stdio server (auto-installed as `skillmem-mcp` entry point). Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "skillmem": {
      "command": "~/dev/skillmem/.venv/bin/skillmem-mcp"
    }
  }
}
```

Tools exposed: `mem_search`, `mem_get`, `mem_write`, `mem_update`, `mem_list`.

## Windows

Одна строка в Windows Terminal (PowerShell):

```powershell
irm https://<your-host>/skillmem/install.ps1 | iex
```

Ставит Python 3.12 (winget) при отсутствии, разворачивает в
`%LOCALAPPDATA%\skillmem\current`, подключает MCP + хуки к Claude Code и
заводит decay/export в Task Scheduler. Откат: `skillmem uninstall`.
