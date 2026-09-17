# Changelog

## 0.11.1

- `mem_archive`: an agent can retire a record that no longer applies — it
  leaves search, recall, list and the hooks' inject, keeps its text, history
  and approval, stays readable by slug, and comes back with `archived=false`.
  Nothing is deleted; deletion stays with the owner at the CLI. A pinned
  record is refused until unpinned. Ten tools.
- Every tool description rewritten to the same shape: what it does, whether it
  writes and what the side effect is, what the parameters mean beyond the
  schema, what it returns, and which sibling tool to use instead and when.
  Where a description promised more than the code delivered, the code was
  brought up to it: `mem_reinforce` now refuses a record that is not a skill
  (strength and decay are a skill's mechanics), `mem_recall` caps `limit` at
  50 as it always said, and archiving writes the lifecycle state and nothing
  else — `updated_at` is the text's age, and archiving is not an edit.
- Restoring a hidden record (`mem_archive` with `archived=false`) refreshes its
  recency and floors strength at 0.5, so the nightly lifecycle sweep does not
  retire it again the same night; it now reports the state the row actually
  ended in. `mem_pin` stays out of it: pinning writes the flag and nothing
  else, so strength is never handed out as a side effect of a different verb.
- A dump now carries `lifecycle`, so an archived record does not come back
  active after an export/import round trip — including a record that was both
  pinned and archived, which used to fail the import and land active.
- The conflict message for an existing slug names no parameter at all. It used
  to say `force=True`, which no surface accepts, and then `reason=`, which only
  the update tools carry — the two tools that actually raise it, `mem_write` and
  `mem_learn`, have neither.
- `mem_archive` refuses a record the owner ever wrote or approved, and names
  `skillmem skills-archive` instead. Archiving hides a record from search,
  recall, list and the session briefing while leaving its text, approval and
  origin untouched, so an agent retiring the owner's own rule left nothing that
  a later read would show. The refusal reads a new `owner_seal` column, set once
  and never cleared: `origin` and `trusted_at` both move under an agent's own
  writes (`mem_update` relabels origin to `agent` and drops the approval, by
  design), so a gate resting on those two was one extra call from open. The
  nightly lifecycle sweep exempts sealed records for the same reason —
  `mem_reinforce evidence="failure"` lets an agent walk a record's strength down
  to the floor, which is the slow way to the same place. The seal is set whenever
  the owner writes or approves a record — including a rewrite at the terminal,
  the very path that clears the approval — and it travels in a dump, so an
  export and import round trip cannot launder the records it protects. An agent
  also cannot change a sealed record's kind: the session briefing selects by
  kind, so a relabelled rule leaves it exactly as archiving would. Existing
  databases gain the column and its backfill on first open, whatever their
  schema version, and a database whose column landed without the backfill is
  repaired on the next open. The seal check runs inside the write transaction,
  so it cannot be overtaken by the owner approving the record between the check
  and the write, and a pack removal skips sealed records: an agent can file a
  record under `pack:<name>`, and the owner's own removal would otherwise delete
  it. The kind guard lives in `upsert`, where every surface arrives: guarding one
  handler left `mem_write`, `mem_learn` and the HTTP routes open, and on unchanged
  text that route wrote no history row at all. The seal migration reads before it
  writes and takes no lock when there is nothing to do — on every open, a write
  lock there made `inject`, `recall` and search fail with "database is locked"
  behind any writer. The nightly sweep holds a transaction for the same reason
  `set_archived` does.
- `skillmem inject` names the owner's own rules that an agent has rewritten since
  they were approved. Rewriting clears the approval, deliberately, and the rule
  then leaves the briefing — silently, until now, so a rule relied on for months
  simply stopped arriving. Names only, never the agent's text.
- Importing a dump no longer resets a faded record's strength and recency: the
  restore fires only for a record that is actually archived, not for every
  non-archived dump, which defeated decay on every weekly export/import. The
  importer's own history rows name it ("import") instead of no one.
- Every change that takes a record out of every read writes a row into the
  tamper-evident history, the nightly sweep's own archiving included (the
  active-to-stale step writes none: a stale record still appears in search,
  recall, list and the briefing), with the acting surface stamped by that surface rather
  than taken from the caller (an MCP client supplies its own name). A call that
  changes nothing writes nothing. `skillmem skills-lifecycle` counts every kind
  rather than skills alone and lists the archived slugs, so what is out of every
  read right now is visible where the lifecycle is reported.
- `skillmem pin` on an archived record says so: pinning does not un-archive, and
  archiving is refused while pinned, so the record would otherwise stay out of
  every read without a word. `restore_skill` and `set_archived(archived=false)`
  are now one implementation — the duplicate pair let only one of them learn not
  to hand strength to a record that was never hidden.

## 0.11.0

Two independent reviewers (one on the Claude side, one on the GPT side) read the
whole codebase for the first time, then re-read every fix, forty rounds deep,
each round hunting for what the previous round's fixes broke. The first pass
found its P1s not in the recap hook everyone had been staring at but in the
parts nobody had reviewed end to end: the HTTP server, body files, packs.

**Trust boundary**
- `skillmem trust` and `--untrust` refuse to run without a terminal, and
  `init --claude-code` adds `"Bash(skillmem trust*)"` to `permissions.deny`.
  Both are safeguards against an agent running the command, not an owner
  authentication: a process with write access to the database can still set
  the columns. README says which is which.
- Unapproved memory is framed by one renderer wherever a body, snippet or
  history entry reaches a model — MCP `mem_get`/`mem_search`/`mem_recall`,
  HTTP `/get`/`/search`/`/recall`, CLI `recall` (text and JSON), hooks — with
  the title (and a history entry's old title) **inside the frame**; a previous
  version is framed whatever the current one's approval — approval belongs to
  the words that were approved. Listings (`mem_list`, `/list`) carry `origin`
  and a `trusted` flag with raw titles. `mem_get` no longer returns a raw body next
  to a `trust_warning` key.
- HTTP `/write` on an existing slug demands the same permission `/update`
  does: resubmitting a public rule's exact text as private used to reassign
  its author and visibility and keep the owner's approval. `/learn` requires
  `write_public` for public skills. A soft-deleted slug is not free for anyone.
- CLI `write`/`learn` from a process without a TTY record `origin=agent`, not
  `owner`; HTTP writes record `origin=agent` instead of `unknown`; auto-memory
  imports default to `agent`.
- Pack import never overwrites a row that is not that pack's own; SKILL.md
  symlinks and files outside the pack are ignored; `git clone --`; aggregate
  caps (500 files / 4 MB); a removed pack reinstalls. Vault attachments must
  resolve inside the vault.

**Data integrity**
- Body files written from now on are `<slug>__<hash>[-<db>]+<content32>.md`:
  namespaced per file-backed database (two databases under one home no longer
  share a newly written file) and content-addressed with 128 bits (a new body
  is a new file, so publish-before-commit is safe and an outer rollback cannot
  leave a row pointing at someone else's text). Pre-0.11 files are not renamed
  and stay shared if two databases referenced one. Orphans in a database's own
  namespace are collected on the nightly `decay` run and HTTP `/decay`, under
  the database write lock, with a 60 s grace; a non-lock SQLite error there
  now propagates instead of reading as "nothing to do".
- `kind` is validated on write — `[a-z0-9_-][a-z0-9_ -]{0,31}` after
  lower-casing, trimming and collapsing whitespace — and existing rows are
  normalised once on the next open (a write) — case, runs of space, tab,
  CR, LF, VT, FF and NBSP; a pre-0.11 `visibility` is lower-cased and space-trimmed, and one
  outside `public`/`shared`/`private` becomes `private`, so old rows stay
  updatable. Filters are
  case-insensitive; a filter nothing can match returns nothing. Export refuses
  any path outside its destination (`kind="../../x"` used to write there).
- Export keeps a per-database manifest and prunes the files it wrote last
  time. Use **one destination per database**: two databases exporting the
  same `kind/slug` into one directory overwrite each other, and the first
  0.11 export over a pre-release manifest adopts and prunes that whole list.
  Strength, pin and the access/confirmed/failure counters are written to
  frontmatter now, and a body is written verbatim, so a dump→restore keeps
  exact slugs (`a_b` and `a-b` no longer merge), LF bodies byte for byte (so
  re-importing over an identical approved row keeps its approval — approval
  itself never travels in a dump), counters, pins (when the dump records
  them) and a recorded `origin` — `unknown` included. Importing a plain
  Obsidian note keeps what the row earned.
- A same-text write through HTTP, MCP or the CLI applies only the metadata the
  caller actually sent (library callers such as pack and vault imports keep
  their file-is-authoritative behaviour — a migrated file still sets kind and
  visibility; same-text provenance changes only when restoration is
  explicitly enabled, i.e. a skillmem dump or a file carrying `strength:`): a
  retried `mem_write` without a `kind` no longer turns a trusted skill into a
  note, a same-text `mem_learn` no longer flips a private skill public, and an
  explicit `topics: []` really revokes a shared audience (the lexical index
  follows). A write onto a soft-deleted slug is refused on every channel (it
  used to say OK and stay invisible); a dump restore or a pack reinstall
  revives it. An ordinary update keeps the strength the row earned (only a
  restore — a skillmem dump, or a file carrying `strength:` — sets it); `restem` indexes full
  document bodies; `reinforce` is one relative UPDATE (concurrent confirmations
  no longer lose each other). Known: `reinforce` is **not idempotent** — a
  retried call counts as new evidence; evidence ids are a later release.
- Decay: a fresh skill is measured from its creation, not from "never used";
  one decay step per threshold (the threshold is at least one day), so a job
  run twice does not compound; the
  lifecycle sweep runs even when nothing decays (the CLI used to skip it on
  those runs, so skills sitting at the floor were never archived).
- History chain: `changed_at` is clamped monotonic on every history write
  (a clock stepped back no longer reads as tampering). A row stamped in the
  future pins later stamps to it until real time catches up — by design; a
  warning is logged once when the gap exceeds a day.
- `init_schema` no longer writes on every open (hooks stalled behind any writer).

**Hooks**
- The Stop→`skillmem migrate` hook is gone: it imported the alphabetically
  first project's memory directory on every turn. `init --claude-code` with
  any hooks mode but `none` removes an existing one (backup written) and
  installs the deny rule; `--hooks minimal` means exactly that and nothing
  else. Hand-written memory: `skillmem migrate --source <dir>`.
- Recall context is budgeted per section before framing (a frame can no longer
  be cut in half) and the seen-ledger lists exactly what was emitted (any slug
  without whitespace or `]`).
- Recap: publication fails closed without its lock; Stop recaps of one
  session are serialised by a per-session lock, and the SessionEnd recap waits
  up to 5 s for an in-flight Stop recap and then proceeds anyway (publication
  is compare-and-swap, so neither can clobber the other); Claude Code's
  synthetic string turns stay out of the summary.

**CLI / MCP / scheduling**
- `skillmem --db X init ...` writes `SKILLMEM_DB=X` (absolute) into every
  agent's MCP entry — Claude Code, Codex, Cursor, Windsurf, Gemini CLI,
  opencode. Claude Code, Cursor, Windsurf, Gemini CLI and opencode update an
  existing entry's database in place (JSON, rewritten atomically). Codex's
  hand-written TOML is never edited in place: an existing entry keeps its
  database and `init` prints the one line to set by hand
  (`SKILLMEM_DB = "X"` under `[mcp_servers.skillmem.env]`). `uninstall`
  removes the Codex table only when the result provably equals the old file
  minus that table; otherwise (including a file it cannot parse) it leaves
  the file alone and says so in `warnings`. `uninstall` also removes a
  skillmem hook from a settings.json group it shares with other hooks. Without `--db` an existing entry is left alone everywhere.
  `init` and `uninstall` write config files atomically, through a symlink
  to its target, with mode kept (JSON and TOML alike). `--db` reaches scheduled jobs (re-run
  `schedule install` after upgrading). `uninstall --purge-db` removes the
  DB, its `-wal`/`-shm`, its namespaced body files and the legacy-named
  files it references (a second database referencing the same legacy file
  loses it — split them first).
- HTTP `/write` and `/learn` with `check_conflicts` reported duplicate
  candidates from every agent's records, private ones included — a 409 that
  quotes another agent's private title is a read through the trust boundary.
  Candidates are now the top BM25 matches *among the records the writer may
  read* (the scan walks the BM25 order past hidden rows until it has scored
  its five, so no number of hidden rows crowds out the writer's own
  duplicate). MCP is one principal; unchanged.
- A same-text write that changes `ttl_days` now moves `freshness_until` with
  it, and over HTTP an explicit `ttl_days: null` clears both; before, the new
  TTL was stored and the deadline never came. Re-sending the same TTL does
  not renew; MCP and the CLI cannot clear a TTL (null is not sent, 0 is
  refused) — 0.11.1.
- `/update` and `mem_update` mark the rewritten text `origin=agent`: an
  owner-authored row edited by an agent kept `origin=owner`, so the owner
  would re-trust words they never wrote (approval was already dropped).
- HTTP `/search`, `/list` and `/recall` used to cut the page *before* the
  visibility filter, so another agent's records could crowd the caller's own
  out of the answer (an empty 200 while `/get` found the row); the filter
  now runs inside the ranking — every match is ranked once and the first
  `limit` rows the caller may read come back, bodies read only for those.
  `/get` lists in `links_in` only the backlink sources the caller may read —
  a private record's slug used to show on the public record it linked to.
- `import-vault` no longer follows a symlinked note out of the vault (the
  rule attachments and packs already had): `zshrc.md -> ~/.zshrc` in a
  cloned vault used to land the real file's text in the database.
- `scrub` is idempotent: a value already rendered as `[secret redacted]` was
  matched again on every re-write, so a dump restore, `mem_update` or
  `/update` of such a row grew `[secret redacted] redacted]`, changed the
  content hash and dropped the row's approval (8 of 914 approved rows on a
  real database after one restore over itself; 44 more were lost to body
  files missing from `docs/`, a data condition the dump reports as warnings).
- `skillmem skills rm <pack>` soft-deletes only the rows the import wrote —
  the ones whose slug carries the `pack-<name>-` prefix under project
  `pack:<name>` and are not `origin=owner`, edited by an agent or not; a
  user's own note filed under `pack:<name>` used to go with it.
- Dump file names: a clean slug that already ends in `__<8 hex>` gets its own
  hash too, so it cannot share a file with the sanitised form of another slug
  (one of the two records was silently missing from the dump).
- Windows: `schedule install` wraps the task command in `cmd /c` with
  `SKILLMEM_HOME`/`SKILLMEM_DB` set, so `--db` reaches scheduled jobs there
  as it does under launchd, cron and systemd. Every token is quoted; a path
  containing `%` or `"` is refused (cmd.exe cannot carry them from a
  command line), and a very long home may exceed schtasks' 262-character
  `/TR` limit — loudly.
- `skillmem skills` (strength list) was unreachable behind the `skills` pack
  group — it is `skillmem skills-top`.
- MCP `limit` is bounded (1..100); `mem_write`/`mem_update` no longer advertise
  an ignored `agent` field; "9 tools", not 8. `visibility` is validated on
  every channel (`public`/`shared`/`private`); HTTP omits it to mean "keep".
- `init --claude-code` run from a new venv (a moved install, pip → pipx) repoints
  the hooks it already wired instead of adding a second copy, and collapses an
  install that is already doubled to one copy of each hook (per event, matcher
  and arguments) — two copies of the Stop hook recapped every session twice. A
  hook the user scoped to another matcher is left alone; missing, `""` and `"*"`
  are the same scope. The `mcpServers.skillmem` command follows the venv too
  (Claude Code only, when it is named `skillmem-mcp` and the new binary exists).
  Config backups (every agent's file, Claude Code to opencode) are byte-exact,
  mode 0600 on POSIX (`~/.claude.json` carries the OAuth account), taken only when a
  file is actually rewritten — a no-op re-run leaves none, a refusal (invalid
  JSON/TOML) leaves the file untouched and none — and named exclusively
  (`.bak.<sec>`, `.bak.<sec>.1`, …), so helpers running within one second
  cannot overwrite each other's copy.
  `uninstall --claude-code` also removes the `Bash(skillmem trust*)` deny rule.
- Upgrading: re-run `skillmem init --claude-code` (and `schedule install`) to
  receive the deny rule, the hook prune and the job environment. A row whose
  pre-0.11 `kind` still fails validation after normalisation (non-ASCII,
  over 32 chars) rejects updates until a `kind` is supplied.
- launchd load failures are reported; switching to systemd removes cron
  entries; Windows project-dir naming matches Claude Code's.

## 0.10.8

An outside review of the last four releases found no P1 but six places where a
description promised something the code does not do. A description that lies is
worse than a thin one: the agent acts on it and cannot check it.

- **A metadata-only write is applied instead of silently dropped.** This one was
  a real bug, not a wording problem: when the text was byte-identical, `upsert`
  returned the existing row and reported success while the project, tags or kind
  change went nowhere. Approval still survives — the words did not change.
- **Cross-language search is attributed honestly.** Snowball stems within a
  language; it does not translate. A Russian query finding an English record is
  the optional semantic layer's doing, and the README said otherwise.
- **`auto_reinforce` no longer says "bump strength"** — it contradicted
  `mem_recall`'s own description, which is the one thing about strength this
  project insists on.
- `mem_write` says what really happens on a byte-identical rewrite (the existing
  record comes back untouched, keeping its approval), `mem_get` names the field
  it actually returns (`trusted_at`), and the HTTP server reports the package
  version rather than a hardcoded `0.1.0`.


- **A tool description said something the tool does not do.** 0.10.6 claimed
  `mem_search` excludes session recaps by default — only the CLI does that, the
  MCP tool searches every kind. Aligning the behaviour was tempting and wrong:
  an agent that writes a note with `mem_write` would stop finding its own note.
  The text now says what actually happens and suggests `kind='feedback'` or
  `kind='skill'` when you want rules rather than the diary.
- **Tests now hold the descriptions to the code.** Every factual claim was
  verified against live calls — `mem_learn` writes `origin='agent'` and no
  approval, `mem_recall` refreshes recency without touching strength,
  `mem_update` drops approval when the text changes, `mem_get`/`mem_list` leave
  the row untouched — and the ones that could silently drift are now assertions.


- **Tool descriptions say what the tool does TO you, not just what it is for.**
  A directory's automated review scored our descriptions and the gaps were fair:
  side effects, parameter constraints and "when not to use this" were missing.
  Now `mem_recall` states plainly that it marks what it returns (refreshing
  recency, never strength); `mem_learn` and `mem_write` say they arrive
  unapproved until the owner runs `skillmem trust`; `mem_update` says that
  editing text drops the approval with it; every read-only tool says so; each
  one names its defaults and points at the tool you should use instead. An agent
  reading only the tool list can now get these right first try.


- **The MCP server reports its own version.** `serverInfo` carried the SDK's
  version (`1.30.0`), so a registry listing or a client's debug output told anyone
  looking a version this package has never had.
- **A Dockerfile, checked in.** Catalogues build a container to score an MCP
  server, and a server whose inferred build fails is kept out of their search
  results. Ours is explicit and verified: image builds, `initialize` answers with
  the real version, `tools/list` returns all 9 tools over stdio.


- **A fresh database is no longer flagged for a lexical rebuild.** Found while
  verifying a real `pip install skillmem` on Ubuntu: a brand-new database reported
  `lexical_reindex_pending: true`, which sends the nightly job on a pointless pass
  and makes `doctor` look alarming on a clean install. The flag is now set only
  when there is something stored to rebuild.


0.10.2 fixed the query side of lexical search; this fixes the index side, which
was the deeper half of the same defect.

- **Two-character tokens are indexed.** The stemmer dropped anything shorter than
  three characters, so `db`, `py`, `js`, `ci`, `ui`, `go` were missing from every
  stored memory — a search for `db.py` could not match however well the query was
  tokenised. In this domain those two letters are the meaning.
- **The rebuild is a scheduled job, not a migration.** The stemmed column is
  derived, so the fix only reaches memories already stored by rebuilding it — and
  that takes about a minute on 8917 rows, while `init_schema` runs inside every
  hook under a 10-second timeout. Schema v11 therefore only sets a flag;
  `skillmem decay` (the nightly job) clears it, `skillmem reindex-lexical` does it
  on demand, and `skillmem doctor` reports whether it is still pending.


- **Lexical recall on a file path was dead without the semantic extra.** The FTS
  query was split on whitespace, so `/work/analysis.ipynb` became a single phrase
  token that matched nothing — and `tool-recall` passes the edited file's path as
  its query. On a plain `pip install skillmem` (BM25 only, the default) that meant
  no recall at all for Edit, Write or NotebookEdit. Queries are now tokenised the
  way the index is, and the test runs with the embedder disabled so CI catches a
  regression instead of the semantic layer hiding it.


Documentation and listing metadata only — no code change.

- The README explains what 0.10.0 changed and how this differs from the memory
  products built for conversational or user memory, with the measured retrieval
  numbers next to the claim. The PyPI page renders the README, so it needed a
  release to catch up.
- `server.json` (MCP Registry), `plugin.json` and `.claude-plugin/plugin.json`
  were still pinned at 0.9.2 — the registry entry would have pointed installers
  at a version that still contains the recursion fixed in 0.9.3.
- The hooks table documents the SessionEnd recap, the rate limit, and the frame
  around unapproved memory; the tool count reads 9, as it has been since 0.9.x.


**Memory now carries where it came from, and trust is something the owner grants.**

The loop this closes was real and open: an external text — a README, a web page —
reaches a transcript, a model distils it into a note, and the note comes back in
the next session under a heading that reads like the user's own rules. Worse, a
document could talk an agent into saving a rule through `mem_learn`, and that
rule then looked exactly like one a human wrote.

- **`origin` on every memory** (schema v10): `owner`, `agent`, `imported`,
  `derived`, `unknown`. Writers declare it; nothing guesses.
- **Trust is explicit.** `trusted_at` is set only by the owner — `skillmem trust
  <slug>` (`--untrust` to withdraw) — and editing an approved memory's text drops
  the approval with it. `origin` alone never confers trust: an agent can be
  talked into storing a rule by the document it was reading. A CLI write approves
  itself only from a TTY, because an agent can call the CLI through Bash as easily
  as a person can type it.
- **One frame at read time, on every channel.** Unapproved memory arrives inside
  a marked block that says it is data, not instructions, with its provenance
  (`origin=derived session=…`, `origin=imported pack=…`) on the line. The frame is
  applied when the text is read, not written into the note: recall collapses
  newlines, session-history truncates, snippets cut the middle, and a summary can
  contain closing backticks of its own. It covers `auto-recall`, `tool-recall`,
  `session-history`, `mem_recall`, `mem_get`, `cat`, and `inject` (which shows
  approved titles only and reports the rest as a count).
- **The summariser runs caged, or not at all.** The child `claude -p` gets
  `--tools ""` and `--strict-mcp-config`, and if a CLI does not understand one of
  those, the recap is skipped with `skip:unsafe-cli` rather than run without them.
  `--no-session-persistence` is hygiene, not safety, and may be dropped.
- **Migration:** additive, inside one transaction, with a copy of the database in
  `<data>/backups/pre-v10-*.db` first, and columns re-checked under the lock so
  two processes opening the same database cannot collide. On the machine this was
  developed on: 8901 rows, 0.45s.
- **Grandfathering, stated plainly:** existing `owner` and `agent` rows are
  approved by the migration (`trusted_by='migration-v10'`) — the alternative is
  that every rule you have relied on for months arrives unapproved the morning
  after an upgrade. Imported packs and transcript summaries are **not**
  grandfathered; on that machine 7809 summaries stayed unapproved.
- `export` writes `origin` into frontmatter and `import_vault` / `import_dir`
  accept `default_origin`, so provenance survives a round trip. A file may lower
  its own origin but never raise it, and trust is never importable.

**What this does not do:** a frame makes the boundary legible, it does not
guarantee a model ignores an instruction inside data. The guarantee comes from the
reader having no tools — which is why the summariser has none.

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
