# Changelog

## 0.9.3

- **The Stop hook no longer recaps its own recaps.** `session-recap` spawns
  `claude -p` to write the summary, and that child is a Claude Code session
  too: with no opt-out in its environment its own Stop hook recapped the recap,
  and every generation spawned the next one. One machine went from 6 recaps a
  day to 4083 in a day — thousands of ghost sessions, a gigabyte of transcripts
  and a burned subscription quota. The child now inherits
  `SKILLMEM_NO_RECAP=1`, and a test asserts the flag reaches it (the old test
  only covered the flag arriving from outside, which is why the regression
  shipped green).

## Unreleased

- **`skillmem skills add <repo>`** imports third-party skill packs (ponytail,
  unlazy, addyosmani/agent-skills — anything shipping `SKILL.md` files) into
  the same database, where they stop being files loaded on every session and
  become ordinary skills: recalled when relevant, confirmed by outside
  evidence, faded out when they never help. Nothing from a pack is executed —
  only `SKILL.md` is read; repo, commit and licence travel with every skill;
  imports are tagged `untrusted-origin` and carry a visible provenance block.
  Per-agent copies of one skill (`skills/x/` vs `.openclaw/skills/x/`) import
  once. `skills ls` shows per-pack strength, confirmations and failures;
  `skills rm` removes a pack (soft delete, history kept).

- **Strength is earned, not claimed (schema v9).** `mem_reinforce` takes an
  `evidence` argument: `self_report` (the default, and what plain retrieval
  produces) refreshes recency without touching strength, while `test_passed` /
  `diff_accepted` / `user_confirmed` raise it and `failure` lowers it (×0.7).
  An agent can no longer reinforce its own mistake by declaring it useful.
  New `confirmed_count` / `failure_count` columns keep the two signals apart.
- **`mem_pin` / `skillmem pin`** exempt a skill from decay and archiving, for
  rules that matter precisely because they are rarely needed (a deploy gate, a
  safety constraint) — where going unused is not evidence of being useless.

- Four more agents share the one database: `init --cursor`, `--windsurf`,
  `--gemini` (Gemini CLI) and `--opencode`, plus `--all-agents` for every
  agent at once. Cursor, Windsurf and Gemini CLI take the Claude-shaped
  `mcpServers` map; opencode gets its own `mcp` block with an argv command.
  Entries are idempotent, backed up, and stamped with `SKILLMEM_AGENT` so
  authorship survives in a shared database; `uninstall` removes them
  (`--no-editors` opts out).

- Distribution packaging (no code changes): Claude Code plugin
  (`.claude-plugin/plugin.json` + `hooks/hooks.json` + single-plugin
  marketplace), MCP Registry manifest (`server.json`,
  `io.github.liza-studio/skillmem`), and the publishing runbook
  `docs/PUBLISHING.md` (PyPI → MCP Registry → plugin).

## 0.9.0 — first public release

Self-improving skills for Claude Code, extracted from an internal agent-memory
project and released under Apache-2.0.

- Skill learning loop: `mem_learn` / `mem_recall` / `mem_reinforce` with an
  Ebbinghaus strength model, scheduled decay, and an active → stale → archived
  lifecycle (archived skills are snapshotted to JSONL, never deleted).
- Hybrid local search: FTS5 BM25 with Snowball stemming (English + Russian)
  fused via Reciprocal Rank Fusion with optional local ONNX embeddings
  (`fastembed`, multilingual, cross-lingual RU↔EN). No cloud, no API keys.
- MCP stdio server with 8 tools (`mem_search`, `mem_get`, `mem_list`,
  `mem_write`, `mem_update`, `mem_learn`, `mem_recall`, `mem_reinforce`).
- 6 Claude Code hooks: mcp-guard, briefing inject, session-history,
  verify-gate (bilingual triggers), auto-recall, tool-recall — plus a Stop-time
  session recap and auto-migration of session notes.
- Tamper-evident SHA256 hash-chain over the edit history (`skillmem verify`).
- Secret scrubbing on every write (API keys, PEM blocks, JWTs, tokens,
  password assignments).
- Provenance guarantees: unique slugs, refusal of silent overwrites,
  near-duplicate detection, reasons required for updates and deletes.
- Markdown round-trip: `export-all` / `import-vault` — your data stays plain
  markdown with YAML frontmatter.
- Cross-platform installers (`install.sh`, `install.ps1`) and scheduled
  maintenance via launchd / schtasks / cron (`skillmem schedule`).
- Optional multi-agent HTTP server with bearer tokens and per-agent
  visibility scoping (`skillmem serve`).
