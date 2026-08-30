# Changelog

## 0.8.1 — 2026-08-07

### upgrade → приватные GitHub Releases
- Публичная раздача (twin-хост) закрыта по решению владельца; `skillmem upgrade`
  теперь ходит в GitHub API (`releases/latest` репо liza-studio/skillmem)
  с токеном и ставит релиз офлайн-путём (тарбол+sha+инсталлер как приватные
  ассеты, SHA256 fail-closed при наличии .sha256).
- Токен: env `SKILLMEM_GITHUB_TOKEN` → файл `<data>/github_token`
  (`skillmem token set|status|clear`) → `gh auth token`. Нужен fine-grained PAT
  с правом Contents:Read на репо. `token status` не печатает значение.
- `install.ps1 -Token <PAT>` сохраняет токен при установке — апгрейды у друга
  работают сразу.
- Легаси-канал (self-hosted latest.version/install.sh) остаётся при явных
  `--url/--version-url` или env SKILLMEM_*_URL.

## 0.8.0 — 2026-08-07

### Windows support — «одна строка в терминал»
- **`install.ps1`** — установщик для Windows Terminal / PowerShell 5.1+:
  `irm https://<host>/skillmem/install.ps1 | iex`. Ставит Python 3.12 через
  winget при отсутствии, venv, SHA256-проверку тарбола, шимы в
  `%USERPROFILE%\.local\bin`, `init --claude-code`, `schedule install`.
- `cli.py`: console-scripts ищутся платформенно (`Scripts\skillmem-mcp.exe`
  на win32 вместо `bin/skillmem-mcp`) — раньше init писал мёртвый путь в MCP.
- Квотинг команд хуков: POSIX — shlex, Windows — `list2cmdline` (одинарные
  кавычки shlex для cmd.exe не существуют).
- `upgrade` на win32 скачивает `install.ps1` и перезапускается через powershell.

### Хуки переехали внутрь пакета: `skillmem hook <name>`
- Порт всех bash-хуков (`~/.claude/hooks/*.sh`) на Python — работают на
  macOS/Linux/Windows без jq/sed/perl: `auto-recall`, `tool-recall`,
  `session-recap`, `session-history`, `mcp-guard`, `verify-gate`.
- recall/search зовутся напрямую через storage (минус два fork'а CLI на каждый
  промпт), stdout принудительно UTF-8 (Windows-консоль cp1251/cp866).
- **Fix:** фильтр feedback `.rank < -5` в bash-версии был мёртв с гибридного
  поиска v0.6 (rank стал позицией 1..N) — секция feedback молча не инжектилась.
- `session-recap`/`session-history` больше не хардкодят каталог памяти — он
  выводится из `transcript_path` (работает для любого юзера/проекта).
- `init --claude-code --hooks full|minimal|none` (default `full`) регистрирует
  весь набор в `settings.json`; дедуп по полной команде, matcher для PreToolUse,
  recap с таймаутом 95с. `minimal` = поведение <=0.7 (только Stop→migrate).

### `skillmem schedule install|remove|status`
- Кроссплатформенное расписание вместо ручных launchd-плистов: decay ежедневно
  04:15 + export-all еженедельно вс 04:30. darwin → launchd, win32 → schtasks
  (`SkillMem\Decay`, `SkillMem\Export`), linux → crontab (маркер `# skillmem:`).
- `uninstall` снимает задания best-effort.

## 0.7.0 — 2026-08-06

### Security — HTTP layer access control
- `/recall` leaked **private skill bodies to every authenticated agent**.
  `recall_skills()` never returned a `visibility` key, so the server's
  `r.get("visibility", "public") == "public"` filter was always true — while
  the DB column defaults to `private`. It now returns `visibility`/`agent`/
  `topics` and the endpoint reuses the same `_visible_to()` check as `/search`.
- `/recall` reinforced skills the caller could not see. Reinforcement now runs
  **after** the visibility filter: strength is no longer a side channel into
  another agent's memory.
- `/update` gated writes on *read* visibility alone — any agent could rewrite
  public team memory and take over its authorship. New `_may_write()` requires
  `write_public` for public rows (matching `/write`), ownership otherwise, and
  authorship now stays with the original author.
- First tests for the HTTP layer (`tests/test_server.py`, 14 cases): auth,
  visibility matrix across four agents, and a regression test per bug above.

### Fixed — data integrity
- Externalized bodies (>8KB) were written **inside** the write transaction: a
  later failure rolled the row back while the new text was already on disk.
  Bodies are now staged and published only after COMMIT, and discarded on
  rollback (`tests/test_body_files.py`).
- A concurrent insert of the same slug raised a raw `sqlite3.IntegrityError`
  instead of `MemoryConflict` — same situation as the pre-check, now the same
  exception.
- `load_body()` silently returned the truncated excerpt when the body file was
  missing. It still does (reads must not break), but now logs a warning.
- `migrate`/`import-vault` died with `UnicodeEncodeError: surrogates not
  allowed` on frontmatter written as a JSON-escaped scalar (`ensure_ascii=True`
  turns an emoji into an escaped surrogate pair that PyYAML does not
  recombine). New `desurrogate()` repairs pairs and drops lone halves.

### Changed — concurrency and resources
- `PRAGMA busy_timeout` (default 10s, `SKILLMEM_BUSY_TIMEOUT_MS`) — a writer
  meeting a held lock now waits instead of failing instantly.
- HTTP server: one connection per worker thread instead of a new, never-closed
  connection per request. MCP server: one connection per process (was one per
  tool call, ~8 call sites).
- `upgrade` no longer builds a shell string (`curl -sSL {url} | bash`) — the
  installer is downloaded and exec'd directly, so a URL from `--url`/env cannot
  inject shell commands. It also requires an explicitly configured https URL.
- Removed the dead first `init` command: a second definition of the same name
  had silently replaced it.

### Changed — prepared for publication
- Removed `deploy/` (deployment scripts for a specific private host).
- Purged private infrastructure from the tree: server IPs, distribution
  domain, home paths, and the hardcoded agent roster (`tokens-init --agents`
  now defaults to a neutral `admin:master,agent-a:write_public,agent-b`).

## 0.6.1 — 2026-07-15

### Fixed — semantic recall was silently dead
- `embed.py`: pin the ONNX weights to `user_cache_dir("skillmem")/models`
  instead of fastembed's default `tempfile.gettempdir()`. On macOS that is a
  per-boot `/var/folders/...` path the OS purges — once the weights vanished,
  `embed_text()` returned `None` and recall degraded to BM25 **without an
  error**. Override with `SKILLMEM_MODEL_CACHE`.
- `install.sh`: install `.[semantic]` by default (was a bare `-e .`, so
  fastembed never reached anyone) with `--no-semantic` to opt out and a
  fallback to the base install if the extra fails.
- `install.sh`: pre-download the model at install time — the ~220 MB fetch
  used to happen inside the user's first search, which just looks like a hang.

### Fixed — release/version discipline
- `__version__` and `pyproject.toml` had drifted (0.4.1 vs 0.5.0). `upgrade`
  reads `__version__`, so the check was wrong for everyone.
- `upgrade`: compare versions as int tuples via `_version_key()`. String
  compare ranked `"0.10.0"` below `"0.6.0"` — correct only while every
  component stayed single-digit.

### Added
- `doctor` now reports a `semantic` block: `on` / `off (MEM_SEMANTIC=0)` /
  `off — fastembed not installed` / `DEGRADED — model failed to load`, plus
  the cache dir and a fix hint. Graceful degradation needs a loud check —
  its absence is exactly what hid the bug above for six weeks.

## 0.6.0 — 2026-06-09

### Added — Curator + recall traces (Phase 3 + Phase 4 groundwork)
- `find_duplicate_skills()` + CLI `skills-dups`: brute-force cosine near-dup
  detection over skill embeddings (curator candidates, read-only).
- `skill_traces` table + `log_recall_trace()` + `trace_stats()` + CLI
  `trace-stats`: record which skills are surfaced for which query. Idempotent
  CREATE TABLE (no version bump). Raw signal for a future DSPy/GEPA optimizer.
  Wired into the bot's recall path; `helped` column reserved for later feedback.
- Server-side (deployed 2026-06-09): idle-fork `curator.py` (classify leftover
  `[auto]` skills via `claude -p` SKILL/JUNK, archive junk, cron 4:20 MSK with
  an idle guard) and source fix in `skill_learning.py` (`_AUTOLEARN_AGENTS` +
  `_is_junk_trigger` — only procedural agents auto-learn).

### Added — Skill lifecycle (Phase 2, Hermes-inspired)
- Schema v7: `lifecycle` column (active → stale → archived), self-healing guard.
- `sweep_lifecycle()`: stale after 30d idle, archived after 90d idle AND faded
  to the decay floor. Uses `COALESCE(last_accessed_at, created_at)` so a
  never-recalled skill ages from creation. Never deletes; JSONL backup before
  archiving (`backups/skills-archived.jsonl`).
- Archived skills excluded from recall (`_bm25_ids`/`_vector_ids`).
- `restore_skill()` brings an archived/stale skill back to active.
- CLI `skills-lifecycle` (state report) and `skills-restore <slug>`. The `decay`
  command now also runs the lifecycle sweep, so the daily cron covers both.
- `tests/test_lifecycle.py` (5 tests). Deployed to server (schema v9 there) with
  a `decay_sweep.py` cron script running as `claude`.

## 0.5.0 — 2026-06-09

### Added — Semantic hybrid recall (Phase 1)
- `embed.py`: local ONNX embeddings via `fastembed` +
  `paraphrase-multilingual-MiniLM-L12-v2` (384-dim). Sovereign, no external
  API, $0. Title weighted ~3x in the doc text (lifts the right item on
  cross-lingual queries). Graceful: returns None → BM25-only when absent.
- Schema v6: `embedding BLOB` on memory_items. Migration is self-healing
  (ensures the column exists even if an earlier run stranded the version).
- Hybrid retrieval: `search()` and `recall_skills()` now fuse BM25 + brute-force
  cosine via Reciprocal Rank Fusion (RRF). Brute-force is sub-ms at our scale —
  no vector index. `_MIN_COSINE=0.25` keeps irrelevant queries empty.
- Tuning (grid-searched on a RU/EN bench, 9/10 hit@1): symmetric RRF (no vector
  over-weight) + gentle strength tiebreaker (`SKILL_STRENGTH_COEF=0.05`). The
  old `*strength*0.3` over-amplified high-strength skills and hurt accuracy.
- **Fixes cross-lingual recall:** a Russian query now finds an English skill and
  vice-versa — impossible with Snowball-BM25 alone.
- CLI `reindex-embeddings [--all]`; env `MEM_SEMANTIC=0` hard-disables vectors.
- `[semantic]` optional dependency group keeps the base install light.
- `tests/test_semantic.py` (5 tests, auto-skip without the embedder).

## 0.4.1 — 2026-05-30

### Security & fixes
- `scrub()` расширен (Phase 2): маскирует AWS-ключи, JWT, PEM private keys,
  Telegram bot-токены и `password=`/`secret:`/`token:` присвоения — а не только
  `sk-/ghp-/AIza`. Критично для multi-user/shared памяти.
- `migrate`: убран хардкод пути `~/...`. Теперь авто-резолв
  `$SKILLMEM_SOURCE_DIR` → `~/.claude/projects/*/memory` → безопасный фолбэк.
  Чинит падение `migrate` и Stop-hook на не-Mac машинах.

## 0.4.0 — 2026-05-27

### Added — Self-Improving Skills (Hermes-inspired learning loop)
- Schema v5: `access_count` + `last_accessed_at` columns on memory_items
- `reinforce(slug)` — bump strength + access count on skill retrieval
- `decay_stale(days_threshold)` — Ebbinghaus decay for unused skills
- `recall_skills(query)` — BM25 × strength weighted skill search
- **3 new MCP tools:** `mem_learn`, `mem_recall`, `mem_reinforce`
- **4 new CLI commands:** `learn`, `recall`, `skills`, `decay`
- **4 new HTTP endpoints:** `/learn`, `/recall`, `/reinforce/{slug}`, `/decay`
- Skills kind: structured after-action reviews (trigger/steps/outcome/lessons)
- Stats now include `skills` count
- Bilingual skill content recommended for cross-language recall (RU+EN)

### Internal
- E2E learning loop test: 5/5 recall accuracy on real multi-agent scenarios
- Server deployed + schema migrated on <your-server>

## 0.3.1 — 2026-05-21

### Added
- `skillmem upgrade [--check]` — fetches `latest.version` from the release
  host, compares with installed version, and re-executes `install.sh` in
  place via `os.execvp` so the binary can be replaced safely.

## 0.3.0 — 2026-05-21

### Added
- `skillmem init [--claude-code]` — first-time setup: auto-discovers all
  `~/.claude/projects/*/memory/`, migrates them, patches `~/.claude.json`
  with MCP entry, adds Stop hook in `~/.claude/settings.json`. Idempotent.
- `skillmem uninstall [--purge-db]` — reverse of init, keeps DB by default.
- `skillmem verify [--strict]` — checks SHA256 hash-chain over
  `memory_history` (tamper-evidence borrowed from NOM §6).
- `install.sh` — bash one-liner for fresh installs (macOS / Linux), with
  SHA256 verification of the tarball.
- `tests/` — 17 pytest unit tests across storage, hash-chain, init/uninstall,
  and project discovery.
- Schema v4 adds `prev_hash` + `self_hash` columns to `memory_history`.

### Fixed
- Hash chain JSON canonicalisation now runs through Unicode NFC, so the same
  logical record hashes identically on macOS (HFS+ NFD) and Linux (NFC).
- `verify_history` propagates breaks instead of silently healing: a forged
  `self_hash` on row N now surfaces a break on row N+1 too.
- `discover_claude_memory_dirs` tolerates dead symlinks and permission errors.
- `_patch_claude_json` / `_patch_settings_hook` use atomic temp+rename writes
  and refuse to overwrite invalid JSON (backup it instead).
- Hook command strings use `shlex.quote` — paths with spaces no longer break
  or inject shell metacharacters.
- `install.sh` validates that `python3.12` in PATH actually reports 3.12+.

### Internal
- Cycle 4 deep audit completed; 11 findings addressed (4 critical, 4 high,
  3 medium).
- Composite score 7.6 → 8.2 (testability 5 → 9, correctness 9 → 9.5,
  security 8 → 9).

## 0.2.0 — 2026-05-21 (internal)

- `--serve` FastAPI HTTP mode + visibility scopes + bearer auth
- Snowball stemmer preprocessor for FTS5
- External body storage for `kind=document`
- LongMemEval bench harness (hit@5 = 80.0%)
- Round-trip-safe `export-all` ↔ `import-vault`

## 0.1.0 — 2026-05-20 (internal)

- Initial MVP: SQLite + FTS5, 5 MCP tools, 12 CLI commands, 87-file
  migration from Claude Code auto-memory.
