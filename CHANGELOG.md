# Changelog

## 0.9.9

Two defects a third review pass reproduced — both in code shipped this week.

- **Publishing a recap is now compare-and-swap.** The freshness check and the
  file replacement were two steps: a Stop that passed the check before
  SessionEnd wrote the final recap would replace it afterwards. The basis is
  re-read inside a short publish lock (milliseconds, not the model call), so the
  late writer sees the final note and stands down.
- **Excluding session recaps from `search` happened after the candidate pool.**
  The pool is capped at 50 per signal, so a wall of recaps filled it and the
  search returned nothing while a matching skill sat just below — `--kind skill`
  found it. Kinds are now excluded inside the ranking query itself
  (`exclude_kinds` on `search` / `hybrid_rank_ids`).

## 0.9.8

- **A rate-limited turn no longer reads the transcript.** The line count ran
  before the rate-limit check, so every Stop — most of which are skipped —
  streamed the whole session transcript first. On one machine those files run to
  59MB, and there were 840MB of them.

## 0.9.7

- **Memory arrives with a trust boundary.** Anything this machine did not author
  — an imported pack, or a note distilled from a transcript that may itself
  quote a web page — is now injected under its own header that says plainly it
  is data, not instructions. A stored instruction is still an instruction, and
  it used to land in the same block as the user's own rules.
- **`skillmem search` hides session recaps by default** (`--notes` brings them
  back). They accumulate one per session and reached 90% of the words in one
  database, so an unfiltered search returned the diary instead of the rules.
- **`skillmem recap [TRANSCRIPT] [--force]`** writes a recap on demand — the
  Stop hook is rate-limited, and this is how you save the closing minutes
  without waiting. With no argument it picks this project's newest transcript.
- **`skillmem hooks-status`** shows what the hooks have actually been doing:
  runs, skips, failures and the last line per hook, plus the state directory.
  Hooks swallow their own errors so they can never break a session, which also
  means one that silently stopped working looks exactly like one with nothing
  to do.
- **Asking by hand no longer inherits SessionEnd's budget.** The first live run
  of `skillmem recap` timed out at 45s: only the real SessionEnd event lives
  inside that 60s ceiling. A manual run gets the full timeout; the SessionEnd
  one also trims its input to 20KB, because a truncated recap beats one that
  times out.

## 0.9.6

A second review pass over what 0.9.4–0.9.5 actually shipped. The races it found
are the kind that lose the most valuable recap — the last one.

- **A stale recap can no longer overwrite a fresher one.** A slow Stop and the
  SessionEnd behind it overlap; the note now records how much of the transcript
  it was built from (`transcript_bytes`) and a run that read less refuses to
  land on top of one that read more.
- **The final recap survives a busy slot.** SessionEnd used to return silently
  when both parallel slots were taken, losing the session's closing turns; it
  now runs anyway — it happens once per session.
- **The SessionEnd budget is honest.** Claude Code raises that event's shared
  budget to the per-hook timeout but never past 60s, so `init` registers 60 and
  the final recap asks the model for at most 45.
- **The lean-flag fallback only fires on an unknown flag.** Any failure used to
  trigger a second run with MCP and session persistence back on — a network or
  auth error would pay twice. A timeout is never retried, and both attempts
  share one deadline.
- **Slot locks are released by their owner only**, and a stale lock is
  re-checked before it is reclaimed, so a collector cannot free a slot someone
  just took.
- **The closing recap is indexed immediately** instead of waiting for the
  separate migrate hook: hooks on one event run in parallel, and SessionEnd
  registers only the recap.
- **A failed write no longer leaves a temp file behind**, and `SKILLMEM_STATE_DIR`
  now overrides the state directory on every OS — `XDG_STATE_HOME` is ignored on
  Windows, where the test suite was using the real one.

## 0.9.5

Four defects an outside review of the hook path turned up — each one silent.

- **Notebook edits get recall again.** `tool-recall` read `file_path`, but
  Claude Code sends `notebook_path` for NotebookEdit, so the query was empty
  and the hook returned nothing at all for every notebook edit.
- **Session notes are linked to their session again.** The recap writes
  `metadata.source_session`; the importer only knew `originSessionId` /
  `sessionId`, so every imported session note landed with a null session —
  7793 of 7793 on the machine where this was found.
- **The MCP guard's count told the truth.** It reported `len(actual)` of
  `len(expected)`, so a config with extra servers could claim "12 of 10
  expected connected" in the same breath as listing one missing.
- **The per-session recall ledger moved out of the shared temp dir** into the
  private state dir (on Linux without `TMPDIR` it sat in a world-writable
  `/tmp`, where a neighbour could pre-create the file and mute someone's
  recall), and stale ledgers are pruned after seven days — they used to
  accumulate one file per session forever.

## 0.9.4

Everything here is about the Stop hook, which fires after **every** assistant
turn rather than when the session closes. 0.9.3 stopped it recursing; this
release stops it being expensive, and closes what a review of the recap path
turned up.

- **Rate-limited recaps, one note per session.** At most one model call per
  session per `SKILLMEM_RECAP_MIN_INTERVAL` (default 600s), and the note is
  named per session and day, so a later recap rewrites it instead of leaving
  the day full of near-identical copies.
- **The limit counts attempts, not successes.** A failing model used to leave no
  trace, so every following turn bought another call.
- **A non-zero exit is no longer stored as memory.** Any output over 100
  characters — a usage-limit message, a stack trace — became a note and
  overwrote a good recap.
- **SessionEnd gets its own recap**, not rate-limited, so the closing turns of a
  session still reach memory. `skillmem init` wires it; existing installs pick
  it up by re-running init.
- **The parallel-slot semaphore is back** (`SKILLMEM_RECAP_MAX_PARALLEL`,
  default 2), lost in the rename. The env opt-out stops recursion; this stops a
  storm from any other cause. O_EXCL locks, reclaimed by age, so a process
  killed with SIGKILL cannot wedge a slot.
- **The summariser child runs lean:** `--strict-mcp-config` (it was booting
  every MCP server) and `--no-session-persistence` (it was leaving a transcript
  on disk per call — a gigabyte on one machine). Dropped automatically on a CLI
  too old to know the flags.
- **Notes are written atomically** (temp file + replace): a crash mid-write left
  a half-written note behind.
- **A typo in an environment variable no longer breaks the CLI.** `int()` on
  `SKILLMEM_RECAP_TIMEOUT` ran at import, so one bad value made every command
  fail, not just the recap. Values are parsed safely and clamped.
- **Tests no longer write into the developer's live state directory**, and a
  stamp left by one test no longer silently debounces the next.

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
