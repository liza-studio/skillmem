# Changelog

## 0.13.0

Rules an agent saves for you now get approved, instead of sitting unapproved
for weeks — and recall finds your curated notes under a pile of session recaps.

- **Approve in chat.** A new Stop hook, `pending-rules`, asks the person (as a
  `systemMessage`, in Russian and English) about every `user`/`feedback` rule
  the last turn saved, at most three, in full, read from the database. If their
  very next message is just `одобряю` / `approve`, the UserPromptSubmit hook
  approves exactly that text (`trusted_by='owner-prompt'`) and keeps the word
  from the agent. Any other reply withdraws the offer. Longer rules are left for
  `review`. A second owner signal, documented with its limits in INVARIANTS §0.
- `pending-rules` imports the auto-memory `.md` files the turn changed, so a
  rule reaches the database (and recall) before the session ends instead of on
  a manual `skillmem migrate`.
- **`skillmem review`** walks every unapproved `user`/`feedback` memory
  (`--types` for more), shows it in full like `trust`, and asks y / n / q.
  Owner-only. The session briefing now asks the agent to tell the user about it.
- **Recall ranking.** Session recaps (`note`/`session`, ~90% of a long-lived
  database) get half their RRF score: they retell every topic and pushed the one
  curated note that answered to #28. Own 30-question set hit@3 18 → 26/30;
  LongMemEval hit@5 unchanged (0.889).
- **tool-recall** runs lexical-only: loading the embedder cost ~1.2 s on every
  Bash/Edit/Write call (1.45 s → 0.2 s).
- `init --claude-code` adds the Stop hook and deny rules for `review`, running
  hooks from Bash, and `owner_prompt`.

## 0.12.1

Packaging only; no change in behaviour.

- The plugin ships an Agent Skill, `skills/skillmem/SKILL.md`: when to recall,
  learn and reinforce, the nine tools, and the trust model.
- `.claude-plugin/plugin.json` no longer names `hooks/hooks.json`, which Claude
  Code loads from its default location anyway; the same five hooks load.
- `.claude-plugin/marketplace.json` has a description, which
  `claude plugin validate` warned was missing.

## 0.12.0

A correctness release. Several things that used to succeed quietly are now
refused or reported, so read "Before you upgrade" first.

### Before you upgrade

- **Owner-only commands need you at a terminal.** `trust` and `--untrust`,
  `rm`, `skills-archive` (both ways), `skills-restore`, `skills rm`,
  `import-vault` and `uninstall --purge-db` refuse without a TTY. MCP, HTTP
  and pack imports never count as the owner, even in your terminal.
- **Sealed records (written or approved by you) are yours.** An agent cannot
  change their text or metadata, pin or unpin them, restore them from the
  archive or revive them after you deleted them; the refusal says to write a
  proposal under a new slug. Failure reports, the nightly sweep and `decay`
  no longer weaken or stale a sealed rule.
- **Re-run `skillmem init --claude-code`** for the new deny rules
  (`skills-restore`, `--purge-db`, `--db`, and `$`, backticks or `eval` next
  to `skillmem`). They match substrings, so they also refuse your own
  commands through Claude Code that mention them in a `skillmem` directory.
- **Approval covers the kind as well as the text.** A write that changes only
  the kind drops approval unless you make it at a terminal.
- **Writing over an archived record is refused** ("restore it first")
  instead of succeeding invisibly; a pack update over one is reported skipped.
- **Imports change only the fields their file states.** `migrate`,
  `import-vault` and `skills add` no longer clear fields a file does not
  mention. `--kind`, the `note` fallback and a subfolder's project apply to
  new records only, and neither default decides an existing record's origin
  any more: a re-imported session note stays `derived` instead of becoming
  your sealed `owner` record, and a feedback rule is no longer relabelled
  `derived`. Case and whitespace variants such as `Note` also stay
  `derived` in both importers, instead of becoming `agent` or a sealed
  `owner` note (INV-14). `import-vault` restores a file as a whole skillmem dump
  only when it carries `exported_at` as well as `metadata.node_type: memory`.
  A Claude Code auto-memory has only the marker, so it is imported as a note.
  It used to reset the fields it does not state (visibility, tags, topics,
  project, TTL, strength) and raise an agent's record to `origin: owner`.
  Skipping auto-memories (the library's `import_vault` default and
  `--skip-frontmatter-memories`) no longer skips every dump too, which
  restored nothing and exited 0 (INV-06).
- **Imports refuse values they used to coerce, and exit 1 if any file
  failed** (`migrate`, `import-vault`, `skills add`,
  `init --migrate-existing`): `ttl_days: tomorrow` or `7.0`, `strength: null`,
  `visibility: ""`, `project: yes`, a mapping as `tags`, a null inside a list
  (`tags: [ops, null]`, which the library refuses), frontmatter or a
  `metadata` key that is not a mapping (`metadata: feedback` became a note,
  never a rule). The other files are still imported.
- **`migrate` reads links the way `import-vault` does:** a link leading out
  of its directory, nowhere, or to a directory is reported (exit 1) instead
  of read or passed over. So is a subfolder, which `migrate` does not
  search; its notes were left out with exit 0. Both import a note once however many links name
  it, in whatever case or Unicode form (INV-08, INV-11).
- **A folder an importer cannot read is reported:** `import-vault`,
  `migrate` and `skills add` name it "cannot be read; not imported" and exit
  1, where they passed over it and exited 0. An unreadable `SKILL.md` is
  reported skipped instead of aborting the pack (INV-08).
  Auto-discovery also reports an unreadable `~/.claude/projects` or an
  inaccessible parent through the import failure reports: `migrate` and
  `init --migrate-existing` exit 1 instead of treating it as no memories.
- **Unresolved vault attachments fail the note:** unreadable, missing,
  unsupported or out-of-vault body embeds are reported with exit 1, just
  like unresolved frontmatter attachments, instead of silently disappearing
  from the import and its backup. An explicit `attachments` list still
  overrides body embeds (INV-08, INV-14).
- **`write` at a terminal approves only a kind you name:** over a record an
  agent created, without `--kind`, it no longer approves the agent's kind;
  `trust` shows it and approves both (INV-01).
- **MCP and HTTP clear a field the same way:** a present key names the
  field and `null` clears it. An empty or null kind or visibility is refused
  everywhere, and HTTP no longer coerces types (`ttl_days: true`).
- `skillmem write --agent` without a TTY is refused on an existing record;
  `--limit` below 1 is refused.
- **Export directories have one owner.** A second database exporting into
  another's directory is refused instead of overwriting its backup. A
  database rebuilt from its dump after the original was lost is refused until
  you run `skillmem export-all <dir>` yourself once.
- **A copied database file** (`cp`, SQLite backup) copies the document bodies
  it shares with the original when first opened, and refuses to open if it
  cannot (read-only copy, missing body).
- **Scheduled jobs** for a database other than the default are named per
  database (see Known issues). Concurrent cron installs and removals now
  share a user-wide lock, preserving other databases' jobs even with
  different `SKILLMEM_HOME` settings (INV-12).
- **A GitHub token is tied to one repository.** A token file from 0.11 is
  sent nowhere: store it again with `skillmem token set --repo OWNER/NAME`.
- Opening an old database repairs out-of-range strength and TTL and invalid
  kinds (`how/to` becomes `how-to`), and stores an empty project, author or
  session (`write --project ""` in 0.11.3) as none, the one a backup restores.
- The recall hooks' "already shown" ledger has a new format; the old one is
  ignored, so an open session may see a rule once more.
- Library: `MemoryItem.confidence` and `supersedes_id` are removed.

### What is now guaranteed

[`docs/INVARIANTS.md`](docs/INVARIANTS.md) states sixteen invariants, which
function enforces each, and their status at this release (section 6);
`tests/properties/` checks them with hypothesis and real concurrent writers.
A release needs zero P1/P2 findings in three classes (section 7):

- **data**: no record, field, history row, body file or backup is lost,
  overwritten or silently not written; export → `import-vault` → export
  gives the same bytes.
- **trust**: only the owner at a terminal approves or seals; a sealed record
  changes only by the owner; unapproved titles and bodies reach a model only
  inside the frame, and an excerpt is never served as the verified text.
- **concurrency**: every read-then-write decision is made inside the write's
  transaction or by compare-and-swap.

### Fixes

- The session-recap debounce holds when the stamp reads a few milliseconds
  ahead of the clock, as a just-written file can on Windows; it let a
  second recap (and model call) through.
- Python 3.11–3.13 and Windows: `migrate`, `init --migrate-existing`,
  `import-vault` and `skills add` report a folder they may list but not
  search instead of stopping with a `PermissionError` (before 3.14,
  `Path.is_dir()` raised there) (INV-08). On Windows, `inject` writes `\n`,
  not `\r\n`; an attachment is stored as `assets/ab/….png`, not
  `assets\ab\….png`, so it round-trips; `import-vault` names a failed file
  `a/b.md` as on every other OS; an export recognises its own dump that a
  checkout converted to `\r\n`; a command closes its database when it ends,
  and the HTTP server when each request ends (it kept one connection per
  worker thread open for its lifetime), so a process that runs them (a test
  runner, a tool) can delete the file; and
  `uninstall --purge-db` deletes the database before its body files and stops
  with a message while another process holds the database open, where its
  records were left without their text (INV-06, INV-10, INV-11).
- Export preserves a complete earlier backup when a concurrent same-text
  repair restores a missing body file after export captured its excerpt,
  including when a case twin changes the dump filename (INV-06).
- Session-history and transcript discovery match names with Unicode
  normalization and casefolding, so `SESSION-*.MD` and `*.JSONL` are found
  consistently across filesystems (INV-11).
- A record renamed to a case or Unicode-normalisation variant of its slug
  (`Foo` deleted, `foo` written) is dumped under its new spelling on APFS
  and NTFS too; the old spelling stayed on disk, listed in no manifest, so
  a case-sensitive copy of the backup kept a stale file no export pruned
  (INV-11).
- A backup restored over a record rewritten since with the same text under
  another kind gives the record its dump's body: a note is no longer left
  stored as a 4 KB excerpt beside a body file. Whether a body goes in a file
  now depends only on its kind and length, and the first open by this
  release (schema v12) moves each body an earlier version placed otherwise
  (INV-06).
- Trust: `upsert` decides every refusal on the row it reads under the write
  lock. On Windows the CLI took `< NUL` for a terminal and stored agent
  writes approved and sealed. `trust` escapes control and invisible
  characters in what it shows, and pins the kind too. New text hashes encode
  title and body unambiguously; old hashes still verify. Opening a database
  that repairs a stored kind (`Feedback` to `feedback`) drops that record's
  approval: an approved note had become an approved, injected rule.
- Frame: a copy of the closing marker is escaped after any Unicode line
  break, with angle-bracket look-alikes (`⨠` and `⪥` count as the two
  brackets they show; `⊁` DOES NOT SUCCEED is one), invisible characters or control sequences. Search output, listings, history and an unapproved record's
  links are framed, and so is a pack's licence line in the `skills add`
  report. A record's links are the words of its stored text:
  `<private>` text inside a `[[link]]` outlived the scrub and was served
  unframed once you approved the record, and a pack update kept the links
  of the text it replaced. Recall applies the per-kind limit after separating
  approved from unapproved rows, so agent records cannot crowd out a rule.
- Reads: search, recall and listings re-check the caller's filter on the row
  they return; `cat`, `mem_get` and `/get` read one snapshot. A missing or
  damaged document body is served as its excerpt with a notice, everywhere.
  MCP tool failures carry `isError: true`.
- Writes: a text edit keeps every field the caller did not name. `learn`
  refuses a slug holding another kind on every surface. `write --body-file`
  and stdin keep CRLF. A database from before schema v5 upgrades instead of
  failing with "database disk image is malformed".
  Interrupted transactions and nested savepoints roll back before propagating
  Ctrl-C, exit or cancellation; reusing the connection no longer acknowledges
  later writes that disappear when it closes (INV-08). A COMMIT that fails or
  is interrupted rolls back too; it left the write lock held the same way.
  A failed or interrupted nested RELEASE also rolls back its scope's writes
  and history, preserving the outer transaction (INV-08).
- Imports: names match case-insensitively on every system, the exact
  spelling first. Attachment and pack lookups share export's Unicode
  normalization and casefolding, so aliases such as `Σ.png` / `ς.png`
  keep their assets in the imported record and backup (INV-11).
  A pack's oversized skills, slug collisions, non-UTF-8 files
  and links out of the pack are reported skipped. A skill whose own folder
  is named `build`, `test`, `dist` or the like is imported, not left out
  unreported.
- Backups: dumps, attachments, the manifest, token files and scheduler files
  are written to a scratch name and renamed. A restore brings back the
  record's times, attachments, session and a cleared deadline. Slugs that
  differ only in case or Unicode normalisation, or are too long for a file
  name, get distinct files. Canonical filename comparisons also protect
  another database's backups and keep pruning from deleting a new dump.
  Body-file GC uses the same comparison, preserving a live document after
  its filename changes Unicode normalisation or case (INV-11).
  An interrupted export's files stay listed as its own. Your export over a
  pre-0.12 manifest takes over only the files that hold your records;
  another database's and unreadable ones stay reserved, not pruned. A lost body or
  damaged attachment no longer replaces an intact backup, and neither does
  an export that finds the write lock busy: it fails instead. A slug made of
  dots gets a file name the restore reads. A restore fails a dump whose
  `owner_seal`, `origin` or `lifecycle` is not a valid value, instead of
  sealing on `'false'` or restoring `Archived` as active. A dump without
  `metadata.type`, `metadata.origin`, `originSessionId` or `strength` keeps
  the record's own value: `--kind` relabelled a stored rule as a document,
  and a missing origin made an agent's record the owner's and sealed it
  (INV-14). That holds when the record's text changed after the backup too:
  the restore put back the old text labelled `owner`, and a pack skill so
  restored was no longer removed with its pack. A library `upsert` that
  changes the text without naming `origin` keeps the row's, and an origin
  it does not name no longer seals the record. Each database's
  weekly export has its own directory (`backups/vault-<tag>` beside the
  default database's `backups/vault`). `uninstall --purge-db` deletes only
  its own body files, never a pre-0.11 file a copy's original still serves.
  A byte copy of a database (`cp`) is another database even at a moved
  original's old path: its export no longer takes over and prunes the
  original's backup, and its GC no longer deletes the original's body files.
  A database moved to another file system counts as a copy: its first
  export into its old directory asks for you at a terminal.
  `uninstall --purge-db` with no database at the default path, or at a
  `--db` path, no longer takes the empty file it opens for the pre-0.12
  database that was there and deletes a moved one's body files. A restore
  committed while an export runs no longer makes the export take another
  database's dump of that record for its own and prune it: the records it
  dumps and the ones it calls its own are one read. An export refuses to publish an
  attachment over a different file another database's export lists, and a
  record's dump never takes an attachment's name (a record of kind `assets`
  replaced `assets/X.md` on macOS and Windows). An export into a directory
  where another database dumped a record under one of its slugs is refused,
  whatever the kind folder: a restore of the directory put that record over
  yours (INV-12). One database file is one database however its path is
  spelled: on macOS `--db M.db` and `m.db` got two namespaces, so the body
  files were copied as a copy's, and a lost one kept the other spelling from
  opening at all. A token file's empty agent name is refused (its own
  records answered 404).
- Concurrency: schema migrations run in one transaction; the HTTP and MCP
  servers check permission and write in one; recap locks are OS locks the
  kernel releases when a holder dies, and a Stop recap reads the rate-limit
  stamp again once it holds its session's lock (two Stops made two model
  calls within the limit); `init` and `uninstall` patch configs by
  compare-and-swap; embeddings are computed after the write lock is released.
  A Ctrl-C while a write waits for another process's lock no longer leaves
  the transaction open, where a library or REPL caller's later writes were
  acknowledged and lost when the connection closed (INV-08).
- Hooks: every hook and `inject` runs inside one fail-open boundary (bad
  input, a broken database, a bad `--db`: exit 0 within the timeout). Hooks
  load the embedding model from cache only; a session's "already shown"
  list lives in the private state directory only, never the shared temp
  directory, where anyone could pre-create it and hide a rule; `auto-recall` reads the first
  2,000 characters of a prompt; the briefing is UTF-8 on every platform. The
  plugin installs the `SessionEnd` recap. `skillmem recap` exits non-zero
  with the reason when it wrote no recap (no `claude` on the PATH, a failed
  model call, a refused index write, a short transcript); it said "recap run"
  and exited 0 (INV-08).
- Setup: `migrate` without `--source` imports every
  `~/.claude/projects/*/memory`; with no terminal it imports nothing and now
  exits non-zero (it exited 0 with a warning, INV-08). `--db X init
  --claude-code` wires the hooks to the database its MCP entry names (they
  wrote recaps into the default one and recalled nothing from X, INV-12);
  a later `init` repoints them rather than doubling them. Cron and systemd entries survive a space,
  `%` or `$` in the data directory. A relative `SKILLMEM_DB` or
  `SKILLMEM_HOME` is made absolute where it is read, so a scheduled job,
  which runs from another directory, backs up the database it was installed
  for (it exported a new, empty one). `upgrade` sends a stored GitHub token
  across a redirect only to the same scheme, host and port.

### Platform and CI

CI runs the whole suite, property tests and hook fuzzing included, on Linux,
macOS and Windows with Python 3.11–3.13, plus a semantic-search job, a build
and a Docker check that the image lists nine tools with stdin held open. The
PyPI release reruns all of it on the tag. `scripts/release-gate.sh` requires
each new test to fail on the commit before its fix.

### Known issues

- The deny rules and the TTY check are not a wall against an agent with a
  shell: `script -qec "skillmem tr''ust x" /dev/null` passes both (INV-02).
- `inject` calls a note `import-vault` sealed "rewritten since you
  approved" it (INV-01).
- A strength an agent lowered before you sealed a record carries into your
  rewrite (INV-03).
- `find_conflicts` also matches archived rows (INV-04).
- A library `import_vault` with no terminal restores a dump's `origin: owner`
  for a kind that is not a note, without the seal (INV-02). Likewise a library
  `upsert` with no terminal stores a caller's `origin="owner"` on a new row,
  unsealed and unapproved (INV-02).
- An SQLite error that ends the whole transaction during one file's savepoint
  in `import-vault` rolls back the files imported before it and commits the
  ones after (INV-08).
- An editor save between the patcher's last check and its rename is lost,
  and so is one of two `skillmem` config patches that run at the same moment.
  Processes opening a brand-new database at the same moment can fail with
  "database is locked" (the switch to WAL does not wait); the database is
  not damaged, and a retry opens it (INV-05).
- Two clients writing the same new slug at once over MCP can get a
  near-duplicate refusal naming the other's, identical, record; `trust` can
  report "no memory" after sealing a record your other terminal removed in
  that instant; `mem_update`'s `history_entries` can count a concurrent edit
  (INV-04, INV-05).
- A restore renames a library-written attachment whose name is not its hash,
  and fails a dump whose attachment is `.txt` or has no extension; an
  attachment lost from both the store and the earlier backup is left out of
  an export without a warning (`verify` does not check attachments), and the
  restore then fails that file;
  a numeric tag from a pre-0.12 library write comes back as a string, and a
  `true` or `null` one fails its record's restore (INV-06).
- Slugs, kind, project, tags, topics, author and session are shown unframed
  (INV-07).
- A slug keeps surrounding whitespace, so `write --slug "x "` makes a record
  `cat x` cannot find. After a restore gives a pack skill another author,
  re-installing the pack refuses it. A CLI write that times out on the lock
  prints a traceback. `migrate` and packs normalise line endings (INV-08).
- `skills rm` skips a pack skill you approved without saying so; the count
  it prints is right (INV-08).
- A hook whose stdout reader has gone exits 120 with a `BrokenPipeError`,
  and one serving records whose body files are missing writes a stderr line
  per record (INV-10).
- A non-default database scheduled with 0.11 keeps its old jobs until
  `skillmem schedule remove` runs against the default database (INV-12).
  External crontab editors do not take skillmem's advisory lock; avoid
  editing the crontab while installing or removing its jobs (INV-05).
- `cat --history` does not show old text; restoring a deleted, archived
  record appends two history rows (INV-13).
- The library `upsert` stores `freshness_until="next week"`, and a float or
  string `created_at`, `updated_at`, counter or recency time, which no
  restore accepts; a note's `ttl_days: "1_0"` is stored as 10 (INV-14).
- MCP accepts a
  visibility such as `" Public "` or `Shared` that HTTP refuses (INV-14).
- Rotating `hooks.log` can lose a line another hook appends during the
  rotation (INV-16).
- The "already shown" ledger is not locked: pruning old session ledgers
  unlinks one after a `stat()`, so a line appended in between is lost and
  the rule is injected again, and `tool-recall` reads, decides and appends
  it unlocked, so parallel PreToolUse hooks can inject the same rule twice
  (INV-05).
- Windows: a `SKILLMEM_VERIFY_PATTERN` that backtracks catastrophically is
  not cut off by skillmem's own watchdog (there is no SIGALRM, and the regex
  holds the interpreter lock the watchdog thread needs); Claude Code's hook
  timeout, 10 s in the shipped wiring, ends it. `uninstall --purge-db`
  refuses while the MCP server (it keeps the database open for its session)
  or an HTTP request in progress has the database open: stop it and run it
  again (INV-10).

## 0.11.3

- Fix recall budget composition emitting the same record twice and dropping
  another. Plan sections before emitting them, account for separators, and keep
  the plan with at least as many records as a plain in-order fill.
- Remove the Docker stdin shim: it waited for EOF before starting the MCP server,
  so clients keeping stdin open never received an initialization response.

## 0.11.2

- The container holds stdin open while the server starts. A scanner that writes
  its requests and closes stdin at once raced the MCP SDK's own 0.4s import: the
  reader did not exist yet when EOF landed, only the first request was answered,
  and a catalogue listing showed zero tools for a server that has nine. Measured
  in the image, cold start, three runs each: without the shim one run answered
  and two did not; with it, three out of three. That claim shipped with a sentence saying ordinary
  clients never took this path; they did, because the shim WAS the
  entrypoint, and 0.11.3 removes it.

- Recall could hand back the rules and throw away the answer. The hook composer
  filled its character budget section by section in order: three feedback rows
  at 400 body chars fill a 1500-char budget, so the skills section no longer fit
  and was dropped WHOLE — header, rows and all. Measured on a four-month corpus,
  this was the default, not an edge case: 63% of everything injected over six
  days was feedback, and skills reached the prompt only when the feedback rows
  happened to be short. Asked what must be set before running pytest in a second
  checkout, the hook returned two generic rules and no skill — while `skillmem
  recall` ranks the skill that answers it first. Retrieval was never the
  problem; the composer discarded what retrieval found. Each non-empty section
  now reserves an equal share of the budget before the sections compete for the
  remainder, so a lone section still gets all of it and neither can starve the
  other.

## 0.11.1

- **Windows: the owner signal was fooled by the null device.** `isatty()` is true
  for any character device there, NUL included, so an agent running the CLI with
  `stdin=DEVNULL` looked like a person at a keyboard and reached every owner-only
  command. The console is now asked directly (`GetConsoleMode`), which fails on
  NUL, a pipe and a file alike. POSIX behaviour is unchanged.

- Every tool description rewritten to the same shape: what it does, whether it
  writes and what the side effect is, what the parameters mean beyond the
  schema, what it returns, and which sibling tool to use instead and when.
  Where a description promised more than the code delivered, the code was
  brought up to it: `mem_reinforce` now refuses a record that is not a skill
  (strength and decay are a skill's mechanics) and `mem_recall` caps `limit` at
  50 as it always said. Nine tools, unchanged.
- **Retiring a record is the owner's, at a terminal.** `skillmem skills-archive
  <slug>` (with `--restore`) takes a record out of search, recall, list and the
  session briefing while keeping its text, history and approval, readable by
  slug. There is deliberately no tool for it: ten review rounds established that
  an agent able to hide a record leaves the owner no trace of it, and every gate
  we built around that was one call away from open. `skills-lifecycle` lists
  what is currently hidden.
- A new `owner_seal` column, set when the owner writes or approves a record and
  never cleared, protects those records from the paths that could hide or delete
  them without anyone asking: the nightly lifecycle sweep, a pack removal, a
  delete, and any change of a record's `kind` (the briefing selects by kind).
  `origin` and `trusted_at` both move under an agent's own writes, so neither
  could carry the rule. Existing databases gain the column and its backfill on
  first open, whatever their schema version; the backfill records completion in
  `meta` and never repeats. Row-level presence used to be the proof, so a row an
  agent legitimately wrote with `origin='owner'` and `owner_seal=0` (no terminal,
  so `upsert` did not mint) was retroactively sealed on the next open. The seal
  is minted in `upsert` alone now, in both branches — insert and same-text — and
  each asks `owner_present()` for itself: a file an agent can write reaches both
  paths, so `origin='owner'` alone is not enough to mint.
- **One owner signal instead of a caller's word for it.** `owner_present()` — a
  TTY — is shared by every surface, because an agent runs `skillmem write`
  through Bash as easily as a person types it. It is accident protection, not a
  wall: a pseudo-terminal is one `pty.fork()` away, and the wall is the
  permission deny rule `init --claude-code` installs for these commands. `skillmem trust` now prints the
  record and asks before approving, pinned to the hash it displayed — an agent
  rewrite between reading a rule and approving it used to become approved text,
  and the hooks then injected the agent's version as the owner's own.
- Every mutation that touches lifecycle, approval, pinning, strength or deletion
  reads and writes inside one transaction, and carries `deleted_at IS NULL`:
  `set_trust`, `set_pinned`, `reinforce`, `decay_stale`, `sweep_lifecycle`,
  `remove_pack`, `soft_delete`, `set_archived` and the same-text metadata write.
  Each was a read followed by a write with a gap an approval could land in.
  Archiving and deleting a sealed record both default to refusing.
- `mem_recall` never fails on its own bookkeeping and waits at most 150 ms for
  it. Recording recency takes a write lock, and behind a writer that turned a
  read the hooks run on every prompt into "database is locked" — and then, once
  it stopped failing, into a ten-second wait on the default busy timeout.
- A `kind` the caller never named is not written. The guard deliberately exempts
  an omitted kind, but the write applied the default anyway, which relabelled a
  sealed record out of the briefing by another route.
- `skillmem trust` refuses a record that is archived: the nightly sweep can retire
  one between the owner reading it and approving it, and approving something out
  of every read is not what they meant.
- The owner check lives inside `set_archived` and `soft_delete`, which ask the
  owner signal themselves. Eleven callers had to remember to pass a flag and the
  eleventh did not, which is why the finding curve went up rather than down. The
  `allow_sealed` parameter is gone from both — a caller-supplied "trust me, this
  is the owner" was one round-12 P1, and the mutation asks `owner_present()`
  directly now.
- Reviving a soft-deleted sealed record needs a person at the terminal.
  `import-vault` used to set `revive=_is_auto_memory(meta)` unconditionally, so a
  forged `.md` file with `metadata.node_type: memory` matching an owner-deleted
  slug brought the record back with agent-supplied title and body — the seal
  survives delete, so the resurrected row read as an owner-approved rule to
  every caller. The refusal is inside `upsert`, where every revive path passes.
- `skillmem rm` and `skillmem import-vault` refuse without a terminal at the
  CLI layer too. Storage refuses a sealed record on its own; the CLI-level gate
  matches the accident protection `skillmem trust` and `skills-archive` already
  had, so an agent invoking these verbs through Bash gets the same "no TTY"
  answer as the other two owner commands.
- The deny rules `init --claude-code` installs also match `python -m skillmem.cli
  <verb>` — a supported entry point that shows up as `skillmem.cli <verb>` on the
  command line, not `skillmem <verb>`, so the bare `*skillmem <verb>*` globs
  missed it. Four new `*skillmem.cli <verb>*` rules cover the four owner
  commands.
- `import-vault` archives nothing without a person at the terminal, and cannot
  mint a seal. Checking the seal was not enough: an import can CREATE the row, so
  a forged `.md` landed a new record already hidden from every read, and a forged
  seal made an agent's own record undecayable and undeletable for good.
- `export-all` marks a body it could only read as an excerpt. `truncated` used to
  mean "the file is missing" alone, so a body that failed its hash check was
  written out as a whole document — and restoring that dump made the excerpt the
  record's real text, dropping the rest of an approved rule from an unattended
  weekly backup. The importer refuses a dump marked that way.
- `skillmem init --claude-code` denies every owner-only command, not just `trust`:
  `skills-archive`, `rm` and `import-vault` reached the same outcomes, and the TTY
  check in each is accident protection rather than a wall. The four verbs are
  denied wherever they land in the command line, not only as a starting prefix — a
  wrap like `script -qec 'skillmem trust x' /dev/null` reads to Claude Code as
  `script...`, not `skillmem...`, and any pty-providing helper (script, unbuffer,
  expect, socat, a python one-liner spawning through pty) reached the same paths.
  The same rules also refuse any invocation that hides the verb behind shell
  substitution: `$V`, `${V}`, `$(…)`, backticks, or `eval` next to `skillmem`.
  `V=trust script -qec 'skillmem $V foo' /dev/null` ran `skillmem trust foo` after
  the shell rewrote `$V`, and Claude Code's fnmatch saw no `skillmem trust`
  substring anywhere.
- An owner rewriting a record through a terminal keeps the approval the CLI
  minted. `_upsert_update_tx` used to clear `trusted_at`/`trusted_by`
  unconditionally on a text change, so the owner's own rule filed itself under
  `awaiting_reapproval` and dropped out of the session briefing until a separate
  `skillmem trust` ran. The write through a terminal IS the approval, so the
  three signals — owner origin, a TTY, and a caller-supplied stamp — carry the
  approval onto the row. An agent overwrite (any of the three missing) still
  resets it: approval belongs to the text that was approved. The same-text
  branch had the mirror hole — the seal was applied but the caller's stamp was
  silently dropped — and now applies both.
- Deleting twice reports the second call as nothing done, instead of appending
  another history row for the same record.
- `import-vault` restores a dump's archived state only with a person at the
  terminal. An agent can write a `.md` carrying `lifecycle: archived` and run the
  import, which is hiding a record by another route; without a terminal the
  record stays visible and the report names it.
- `skillmem trust` refuses a record whose text can only be shown as an excerpt —
  a missing body file, or one that does not match the approved hash. Approving on
  the strength of an excerpt approves text the terminal never displayed. A body
  file with no recorded hash counts as a mismatch, and a tampered file is never
  recorded as the previous version in the history chain.
- An update that touches no row raises instead of reporting success: a record
  deleted between the read and the write used to be acknowledged as stored.
- The HTTP layer answers 403 for a refusal on the owner's own record, rather than
  500.
- One imported file is all-or-nothing. A refusal partway through left the row
  written and its lifecycle, pin and counters unapplied.
- HTTP recall no longer answers 500 when its recency bookkeeping meets a write
  lock, the same fix the MCP path already had.
- An externalised body is checked against the hash of the text that was
  approved, on every read and in `skillmem verify`. The file is
  content-addressed and the row carries the hash, but nothing compared them: one
  file write changed approved words with `trusted_at`, `content_hash`,
  `updated_at` and the history chain all left intact. A body that does not match
  is not served to a model at all; the stored excerpt is served instead.
- `mem_learn` refuses a slug that holds a note **without writing anything**; the
  kind check used to run after the write, so a refused call still landed its
  tags. The conflict message for an existing slug names no parameter at all —
  `force=True` was in no surface's vocabulary and `reason=` is in neither tool
  that raises it.
- A dump carries `lifecycle` and `owner_seal`, so neither an archived record nor
  a sealed one comes back unprotected after an export/import round trip, and an
  import no longer resets a faded record's strength and recency.
- `scripts/mcp_smoke.py` runs anywhere: it seeds what it asserts through the
  server's own stdio, finds the binary through `SKILLMEM_MCP_BIN` or PATH, sends
  the server's stderr to a file (the embedding model's download progress used to
  fill the pipe and stall the server), enforces its deadline with a timer, and
  deletes its own records.

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
