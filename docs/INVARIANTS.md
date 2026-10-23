# skillmem invariants

The specification later work builds on. It records what must always hold and
which function has to enforce it. It also records where today's code (branch
`ideal-refactor` at 5f1800b, version 0.11.3) falls short. Line numbers refer
to that tree. Section 6 gives the status at 0.12.0, and names the places
where this text was itself wrong or incomplete. Section 7 is the bar a
release must clear.

Sources: `skillmem/*.py` read in full; CHANGELOG 0.11.0–0.11.3, including
"Known issues, deferred to 0.11.4"; `tests/test_release_0_11_3_audit.py`,
where every test is a defect that was found (mapped to invariants in
Appendix A). A claim marked **(probe)** was checked by running this tree
with `MEM_SEMANTIC=0` and a temporary home. Everything else comes from
reading the code.

## 0. Terms

- **Owner signal**: `storage.owner_present()` (storage.py:983). It means a
  person is at a terminal. It is the only accepted proof of ownership.
  A caller-supplied flag (`owner_call=`, `item.trusted_at`,
  `origin="owner"`) is not proof. *Added at the third review:* a surface
  only agents use (MCP, HTTP, packs) never has the owner, even when the
  server runs in the owner's terminal and so has a TTY. Every mutation asks
  `storage._owner(surface)`, which says so. *Sixth review:* on the CLI the
  signal is a TTY, which any process with a shell gets from a pty
  (`script`), and the deny rules `init --claude-code` installs match the
  command line before the shell rewrites it (`skillmem tr''ust x` matches
  none). Neither is a wall against an agent with a shell, and nothing here
  claims one; the invariants hold relative to this signal.
  *Approve-in-chat:* `set_trust(owner_prompt=True)` is a second signal with
  one caller, the UserPromptSubmit hook, and only for the exact text the Stop
  hook `pending-rules` just showed the person (`systemMessage`, read from the
  database), when their very next message is just "approve" / "одобряю".
  Claude Code runs hooks, not the agent; the deny rules
  `Bash(*skillmem hook *)` keep the agent from feeding a hook a forged prompt.
  Same strength as the TTY: accident protection, not a wall. The pending list
  lives in the state dir, which an agent with a shell can write; the Stop
  hook rewrites it before every offer, so a forgery has to land after the
  turn ends and before the owner replies.
- **Write lock**: the SQLite write lock that `storage.tx()` takes with
  `BEGIN IMMEDIATE` (storage.py:124). A nested `tx()` is a SAVEPOINT inside
  the same lock.
- **Live row**: `deleted_at IS NULL`. A **visible row** is also
  `lifecycle != 'archived'`. Ranked and listed reads (search, recall, list,
  briefing) return visible rows. By-slug reads (`get`, `cat`, `mem_get`,
  `/get`) return live rows, archived ones included, by design (0.11.1
  "readable by slug"). Export returns live rows and marks archived ones.
- **Text**: `title` and `body`. New text uses SHA-256 of the UTF-8 JSON pair
  `json.dumps([title, body], ensure_ascii=False)`. The old
  `sha256(title \n\0\n body)` delimiter could occur in either field, so
  different pairs had one hash. `_matches_hash` verifies either format with
  the stored title fixed. Under the write lock, `upsert` compares titles too
  before calling a write same-text; a true no-op or repair preserves its old
  hash and approval. Every text change uses the unambiguous new format, so
  an alias cannot satisfy a pending hash-based approval after another edit.
- **Named field**: a field whose key the caller's input contains. For the
  CLI, the option was given on the command line. For MCP and HTTP, the key is
  present in the JSON. For a file, the key is present in its frontmatter. In
  a skillmem dump every field is named.
- **Sealed**: `owner_seal = 1`. It means the record was the owner's. Only
  the owner sets it, and nothing ever clears it.
- **Surfaces**: the CLI (`cli.py`), MCP (`mcp_server.py`), HTTP
  (`server.py`), hooks (`hooks.py`), vault import (`vault.py`), migrate
  (`migrate.py`), packs (`packs.py`) and export (`export.py`).

Status labels: **HOLDS**, meaning no counterexample is known. **GAP**, a
counterexample exists, with evidence. **LATENT**, meaning it holds only
because every current caller happens to behave. A new caller would break
it.

---

## 1. Invariants

### INV-01: Approval is bound to the content hash
**Rule.** `trusted_at` is non-NULL only for the exact `content_hash` the
owner approved: either the hash displayed by `skillmem trust` or the text an
owner wrote at a terminal. Any change to title or body clears it, unless that
change is itself an owner-at-terminal write. *Eighteenth review:* "displayed"
means every character is visible: `trust` printed the text raw, and a
carriage return with an erase-line sequence hid a line the owner then
approved. Its preview shows each control, format and line-separator
character as its escape (`cli._as_seen`). *Nineteenth review:* and each one
that renders as nothing (`hooks.renders_as_nothing`: every format character,
every unassigned code point, variation selectors, the grapheme joiner, the
Hangul fillers, the Khmer inherent vowels, the blank Braille cell); raw
variation selectors carried a command into an approved rule. "The hash
displayed" covers the kind displayed beside it: the kind decides whether
approved text is injected as a rule, and a `note` an agent relabelled
`feedback` while the owner read kept its hash and was approved as one.
`set_trust` compares both under its lock (`expect_hash`, `expect_kind`).
*Twenty-second review:* so the kind is part of what approval is bound to on
every write, not only at `trust`: a same-text write that changes the kind
clears approval unless it is itself the owner's approving write at a
terminal (the owner's routine `migrate` of an agent's relabelled file kept
it, and an approved note became an injected rule). `_resolve` decides it.
*r04 review:* an open is no approving write either: its repair of a kind
0.10 stored unvalidated (`Feedback` to `feedback`, which the case-sensitive
rule query then counts) clears approval in the same `UPDATE` (`_heal`).
*r06 review:* the owner's write approves the kind it names (`--kind`, or
`learn`'s `skill`), a new record's, or one already approved; over an
agent's record it names no kind for, it approves nothing (`write` printed
only `OK: slug (id)`, and an agent's `feedback` label made the owner's own
note an injected rule).
**Enforced by.**
- `set_trust` (storage.py:1300), with `expect_hash` compare-and-set at
  1326–1330.
- `_upsert_update_tx`: `trusted_at = CASE WHEN owner_writing`
  (storage.py:1868–1872, 1894–1895).
- `_upsert_same_text`, which keeps approval on identical text
  (storage.py:1447–1454).
- The insert branch of `upsert` (storage.py:1606).
- `load_body`/`verified_body_file` (storage.py:1209, 1230) serve the
  excerpt, never an unverified file, as approved text.

**Check.** Property test with hypothesis over random operation sequences
(writes, updates and restores on every surface; `owner_present` toggled).
Keep a model `approved[slug] = hash at approval`. After every step, assert
`row.trusted_at is not None ⇒ row.content_hash == approved[slug]`. Also
assert that `load_body(row)` either verifies against `content_hash` or equals
the stored excerpt.
**Status.** GAP. The insert branch writes the caller's `item.trusted_at`
whatever `owner_present()` says (storage.py:1606). A library call
`upsert(MemoryItem(..., trusted_at=5))` with no TTY is stored with
`trusted_at=5, owner_seal=1` **(probe)**. Only the CLI builds such items
today (cli.py:271, 2079, through `_owner_trust`), so the guard sits in the
caller.

### INV-02: Only the owner mints trust or a seal
**Rule.** `trusted_at` goes NULL→non-NULL, and `owner_seal` goes 0→1, only in
a process where `owner_present()` is True. The storage function performing
the write asks for itself. *Twelfth review, one exception the rule did not
state:* opening a database from before approval existed (schema 9 or older)
approves the owner's own notes and the skills its sessions learned
(`origin` `owner` or `agent`, `trusted_by='migration-v10'`), and the
one-shot seal backfill seals them, with no terminal. It is deliberate: an
upgrade run by a hook must not leave every rule the owner relied on
unapproved. It runs once per database, and grants an agent nothing it did
not have: writing a schema-9 file takes write access to the database file,
which can set `trusted_at` directly. It approves the kind the same open
repaired (`Feedback` to `feedback`, *r09 opus review*): INV-01's r04 rule
drops approval a row held, and a schema-9 row held none.
**Enforced by.** The mint sites:
- `set_trust` (storage.py:1331–1335).
- `upsert` insert (storage.py:1612–1613).
- `_upsert_same_text` (1438, 1448–1454).
- `_upsert_update_tx` (1864–1872).
- The vault seal restore (vault.py:402–408).
- The one-shot backfill `_migrate_owner_seal` (storage.py:600–607), and the
  v10 grandfathering `_backfill_origin` (the exception above).

**Check.** Patch `owner_present` to False. Drive every surface (CLI through
`CliRunner` with no TTY, MCP handlers, the HTTP TestClient, `import_vault`,
`import_dir`, `import_pack`, hooks) with arbitrary inputs, including items
carrying `trusted_at` and `origin="owner"` and dumps with
`owner_seal: true`. Assert that no row's `trusted_at` or `owner_seal` goes
from unset to set.
**Status.** GAP.
- The seal is minted on `... or item.trusted_at` (storage.py:1438, 1613,
  1864–1867). That is a caller stamp, not the owner signal. Same evidence as
  INV-01.
- `set_trust` does not ask `owner_present()`. Only its CLI caller does
  (cli.py:405).
- `owner_call=` (storage.py:1478) is the caller-supplied "I am the owner"
  shape that 0.11.1 removed from `set_archived`/`soft_delete`.

Recommendation: ask `owner_present()` inside `upsert` and `set_trust`. Drop
the `owner_call` parameter and ignore `item.trusted_at`/`trusted_by` unless
the owner is present.

### INV-03: A sealed record changes only by the owner
**Rule.** For a row with `owner_seal = 1`, only a process with
`owner_present()` changes its title, body, kind, metadata (project,
visibility, tags, topics, TTL, agent, attachments, source_session), `pinned`
or `lifecycle` (the sweep's `stale` included), or deletes it. No agent's
evidence lowers its `strength`: under tool-recall's floor the rule is hidden,
as an archive would hide it, and neither does the nightly decay. Strength
lives in `0..STRENGTH_CAP`: a write carrying a value outside it is refused.
The session's seen ledger may suppress only records actually emitted by
recall. It records exact slugs from the selected rows, never from parsing
rendered text; an agent-controlled slug must not mark another rule seen.
It lives in the private state directory and nowhere else: with no usable
state directory there is no ledger, and nothing is suppressed (*r04
review:* a fallback in the shared temp directory could be pre-created by
anyone). An open's repairs (`_heal`) are the one change a sealed row takes
without the owner: a kind, visibility, strength or TTL no write accepts is
moved to the nearest one it does, and an empty project, agent or session is
stored as none, so the record's dump restores (INV-06),
and a repaired kind drops approval (INV-01).
*Added at 0.12.0 review:* strength and `stale` were not named, and both were
reachable; *second review:* decay still lowered it; *third review:* an
imported strength above the cap was "capped" down by a `self_report`, and
`reinforce`/`set_pinned` asked the process's TTY, not the surface.
*Fifth review, decided:* `updated_at` is the age of the text and its
metadata, not a field the owner sets, so it is not added to the list. A
same-text write that changes only the columns holding or indexing the text
(`body` excerpt, `body_path`, `stemmed`: a body file repair, a rebuilt
lexical index) is a repair. It is allowed on a sealed row, and it moves
`updated_at` on no row (`_REPAIR`, storage.py): moving it reordered
listings and made a stale rule fresh.
**Enforced by.** The rule should live in one check in `upsert` under the
lock (the `fresh`/`sealed_now` re-reads at storage.py:1641–1682 and
1771–1817), plus `set_pinned`, `set_archived` and `soft_delete`. Today it is
enforced only for:
- kind (`_kind_is_a_change`, storage.py:969, 1674, 1807);
- a pack import (`keep_sealed_text`, 1664, 1803);
- archive (3115);
- delete (2022);
- pack removal (packs.py:348);
- the nightly sweep (storage.py:3028) and decay (`_decay_stale_rows`; at
  0.12.0 inlined into `decay_stale`).

**Check.** Seal random rows. With `owner_present` False, call every mutating
entry point on them. Assert that each either raises
`SealedRecord`/HTTP 403/MCP `isError`, or leaves every column listed above
unchanged.
**Status.** GAP, partly by 0.11.0's design, which let agents edit and
dropped the approval.
- `mem_update`, `/update` and `skillmem write --reason` without a TTY
  rewrite a sealed record's text and metadata. **(probe)**: `mem_update` on
  a sealed, approved rule gives `body='agent text', trusted_at=None,
  owner_seal=1`.
- `mem_pin`, `skillmem pin` and `set_pinned` unpin a sealed record
  (**probe**; known issue K3).
- `skillmem skills-restore` and `set_archived(False)` un-archive with no
  TTY.

Why the rule should hold: an agent rewrite drops the approval, and the rule
then leaves the briefing (storage.py:2726–2732). That is the same "hidden from the
owner" outcome that archive and delete already refuse. The recommendation
is one check in `upsert` under the lock: `fresh.owner_seal and not
owner_present()` → `SealedRecord`, for text change and same-text metadata
change alike. That subsumes `keep_sealed_text` and the kind guard, which can
then be deleted. Add the same check in `set_pinned`. An agent that wants an
owner's rule changed writes a proposal under a new slug.

### INV-04: Reads return live rows as they are at read time
**Rule.** Every row that search, recall, list or briefing returns was
visible in the same statement that read the text returned, and still
matched the ranking's own filter (kind, project, kind exclusion) there.
Every row a by-slug read or export returns was live in that statement.
Filters (visibility, approval, already-seen, minimum strength, kind
exclusion) are applied inside the ranking, before the limit.
**Enforced by.**
- `_fetch_live` (storage.py:2183) for `search` (2518), `recall_skills`
  (3209) and `list_items` (2198).
- `_keep_visible` (2439) for filters before the limit.
- The briefing SQL (2699–2716).
- `get` (2151), `export._iter_all` (export.py:108).
- HTTP rechecks `_visible_to` on fetched rows (server.py:270, 304, 492).
- `/get` in a `snapshot` (server.py:281).
- The hook composer classifies trust by the fetched row (hooks.py:449–481).

**Check.** Monkeypatch the ranker (`hybrid_rank_ids`, `_keep_visible`) to
archive, delete, privatise or rewrite rows after ranking. Assert that every
returned row's text equals its current text and that the row is
visible/live/permitted, for every reader. The audit tests at 1252 and 1268
do this for archive only. Extend them to delete, visibility and approval.
**Status.** GAP. *Corrected after the property tests:* "HOLDS for ranked
reads" was too broad. `_fetch_live` re-applies deletion and archive, not the
caller's `visible` predicate, so a record made private or unapproved between
the ranking and the fetch is returned with its fresh text. Other gaps:
- `find_conflicts` refetches title and body without `deleted_at IS NULL`
  (storage.py:2635). A 409 can quote a record deleted between the walk and
  the fetch. Only callers without the lock are affected.
- `mem_get` and `skillmem cat` read the row, links and history in separate
  statements (mcp_server.py:124–137, cli.py:171–213). HTTP uses a snapshot.
  Move the snapshot into one storage read used by all three.

### INV-05: A read-then-write decision is made in the write's transaction
**Rule.** Any value read to decide a write (permission, seal, kind,
existence, deletion, lifecycle, visibility, previous text for history) is
read under the same write lock as the write. The alternative is a single
statement or a compare-and-set whose `WHERE` re-states the condition.
**Enforced by.** `storage.tx` (storage.py:124) around the decision and the
write. Section 3 lists every site.
**Check.** For each site in section 3, a test in the style of the audit
file: a second connection commits the conflicting change at the point
between read and write (monkeypatch the read function). Assert that the
write either sees the change or is refused. A generic harness can wrap
`conn.execute` so that the first `SELECT` on `memory_items` triggers the
competing commit.
**Status.**
- LATENT: `upsert`'s sealed-revive refusal (storage.py:1555–1567) runs on
  the pre-lock read. It holds only because both revive callers (vault, packs)
  already hold an outer `tx`. (*At 0.12.0:* `_refuse` decides it on the row
  read under `upsert`'s own lock.)
- Low: the hook seen-ledger (hooks.py:570, 152).
- *r11 review:* the export read the records it dumps and the ones it calls
  its own (slug and birth, the proof it owns a manifest entry's files) in
  two statements; a restore committed between them made it prune another
  entry's dump of the restored record and write none. Both come from one
  statement (`export._iter_all`).
- *Twentieth review:* the export read a row, then its body file, with no
  lock; an edit and a body-file GC in between made the dump a `truncated`
  excerpt over the last good one. A body read as an excerpt is read again,
  with its row, under the write lock GC deletes under (`export._current`).
  *Twenty-third review:* a busy lock fell back to the row read before it,
  as a read-only database does, and its excerpt replaced the last good
  dump; only a read-only database falls back, a busy one fails the export.

### INV-06: Export → import-vault → export round-trips
**Rule.** A value an earlier version stored outside what a write accepts
(0.11: strength above 2.0; 0.10: any strength and TTL) is brought into it
when the database is opened, or its own dump is refused on every restore
(*twentieth review*). For any database D, let E1 = `export_all(D)`. Restore E1 with
`import_vault(D', E1)` at a terminal, where D' is D after arbitrary writes or
a fresh database. Then for every record in E1, its file in E2 = `export_all(D')`
is byte-identical to its file in E1 except for the `exported_at` line, and
`get()` returns the same fields, `created_at` included (a time as stated,
0 too: *tenth review:* a fresh restore took `created_at: 0` for none and
stamped the current time), except a seal D' gained
after the backup, which stays (INV-03: nothing unseals) (*third review:* a
restore over another database's record under the same slug kept that
record's birth date beside the dump's age). A 0.11-format `content_hash` (see Terms) comes back as it was
where the restore keeps the row; a row it inserts or rewrites gets the
unambiguous hash of the same text, as every text write does, since a legacy
hash admits an alias pair (*twenty-fourth review:* the rule said equal, which
no restore into a fresh database can give without re-admitting the alias). *Corrected at the second review:* "E2 is
byte-identical" was too broad. A restore merges, so a record written to D'
after the backup survives it and is in E2 too. An empty `project`, `agent`
or `source_session` is stored as NULL, the one "none" a dump can carry; one
0.11.3 stored as `""` is made NULL when the database is opened (*r11 review:*
it came back NULL, and `get()` and the dump differed). That covers body (CRLF, lone CR, U+2028, NBSP,
leading and trailing newlines, `---` lines, documents over 8 KB), exact
title (padding and newlines included), slug, kind, project, tags, topics,
visibility, agent, source_session, attachments (files and list), TTL and
deadline, strength, pinned, archived state, counters, origin and seal, and
the recency clocks `last_accessed_at` and `last_decayed_at` (*sixth review:*
they were not listed and not carried, and decay and the sweep read a record
recalled before the backup as idle since its birth). Every text field holds
NEL (U+0085) too (*thirteenth review:* YAML read one in the frontmatter as a
folded line break, and a title, slug, tag or project came back with a
space). The property compares
every `MemoryItem` field but the row id, the body file name, approval and a
`stale` lifecycle (the sweep derives it, see Status), and
moves each of them off its default (*seventh review:* it compared a stored
`confidence`, which no dump carried, and a deadline cleared beside a TTL,
which a restore derived again, only at their defaults; `confidence` and
`supersedes_id`, which no surface sets or reads, left `MemoryItem`).
`body` is compared too: whether it is the whole text or an excerpt beside a
body file is decided by the kind and length alone (`_body_in_file`), never by
the row's past (*r12 opus review:* a body file was kept on the same text, so
a note restored over a same-text document stayed an excerpt, and the property
left `body` out). The first open by 0.12.0 moves each body an earlier version
placed otherwise.

The exported row and body are fixed before its path and attachment list
are planned. A retry after a concurrent edit and body-file GC must use that
same row for both the document and its assets (*twenty-first review*:
refreshing only the document named a new attachment the export never
copied), whatever the edit changed: a record the edit moved to another kind
or slug is dumped as it now is, and one it deleted is left out, as the next
export leaves it out (*twenty-first review:* those kept the planned row, and
its excerpt replaced the last good dump).

A copy the store lost is not lost from its backup (*thirteenth review*). An
export into the directory of an earlier one keeps an attachment file the
store no longer has, and keeps a document's text when its body file is gone
(an earlier dump of the record holds it if it hashes to the record; *fourteenth
review:* the export looked only at the file it was about to write, and a case
twin had moved the record's dump to another name). A stored attachment whose
bytes do not hash to its name is lost too (*fourteenth review:* it replaced
its intact backup). Backup body recovery is decided on the captured body,
not another read of its file (*r12 review:* a concurrent same-text repair
made recovery skip the earlier dump, then the captured excerpt replaced it
and the complete backup was pruned). A restore
takes a listed attachment from the dump or, under the same stored name, from
the store, each only if intact. One in neither fails the file (INV-14). It is
not dropped from the list.

Approval never travels in a dump, because a dump is a file any agent can
write. It survives the round trip exactly when D' already holds the row with
the same `content_hash` and the same kind. A restore that changes the text
or the kind drops approval (INV-01; *twenty-fourth review:* the kind was
not named), and a fresh database gets none.

The seal a dump states is the seal restored: `owner_seal: false` is not
re-derived from `origin: owner` (*nineteenth review:* an owner record a
library call wrote with no terminal came back from its backup sealed). A
dump from before the seal has no such key, and its origin decides, as the
seal backfill decided. A key every export writes (`metadata.type`,
`metadata.origin`, `originSessionId`, `strength`) names its field by being
there: a dump without it keeps the row's value, and `--kind` or the
importer's origin is an insert default (INV-14; *r16 opus review:* a dump
with no `metadata.type` relabelled a feedback rule `document`, and one with
no `metadata.origin` made an agent's record `owner`, which the owner's
restore then sealed; *r17 opus review:* on a text change it still wrote
the importer's `owner`, since `upsert` set the origin on every text change).
With neither `owner_seal` nor `origin`, the importer's
origin decides the seal only on insert, where it is applied.
*Twenty-third review:* the state a dump records is a
value or the file fails (INV-14): `owner_seal` a boolean, `origin` one of
`storage.ORIGINS`, `lifecycle` `active`, `stale` or `archived`
(`owner_seal: 'false'` sealed, `lifecycle: Archived` came back active and an
unknown origin became the importer's default).

Skipping Claude Code auto-memories never skips a dump (*r09 opus review:*
both carry `metadata.node_type: memory`, so the library's default and
`--skip-frontmatter-memories` restored nothing with exit 0); an auto-memory
is a file without `exported_at` (`vault._is_auto_memory`).

Every file an export writes is one the restore reads (*twenty-third review:*
a slug of dots, `..`, was dumped as `...md`, which has no `.md` suffix, and
the restore passed it over with exit 0). A dump file name never starts with
a dot (`export._safe_filename`).

**Enforced by.**
- `export._frontmatter`/`_write_planned` (export.py:52, 272), which write
  bytes.
- `vault._parse_md`/`_title_from`/`_restore_meta` (vault.py:63, 132, 96).
- `upsert(explicit=<all>, restore_strength, revive)` (vault.py:342–362).
- Post-upsert restores of pin, lifecycle, seal and counters
  (vault.py:363–416).
- `verified_body_file` for `truncated` (export.py:282).
- `export.dump_yaml`, the one YAML writer (NEL double-quoted).
- `export._export_locked` keeps a lost asset's backup, and `_kept_bodies`
  gives back a lost body from any earlier dump of the record, read before the
  first write; `vault._collect_attachments` resolves a listed attachment in
  the dump, then the store, or fails the file. `export.intact_asset` is the
  one check that a stored attachment is present.

**Check.** Hypothesis generates the records above, including slugs that
differ only in case, contain `/`, are unicode or are over 200 bytes. Run
E1 → restore into D and into a fresh DB → E2. Compare the files with
`exported_at` masked. Assert `get()` field equality and `trusted_at`
preservation on same-hash rows. Also assert `export_all` writes nothing to
the database (read-only file).
**Status.** GAP, one line. `updated_at` is written by export
(export.py:72) but not restored (vault.py:112–114 says so on purpose), so E2
differs whenever the restore inserted or changed a row **(probe)**.
Recommendation: restore `updated_at` from a dump, as counters are
(vault.py:409–416). A dump is the whole record, and listing order and
freshness depend on it. `lifecycle: stale` is not carried (export.py:93–97).
The sweep derives it, so it is not part of the equality.

Everything else was checked **(probe)**: the probe's cases (CRLF, a newline
in the title, `---` in the body, whitespace-only title, a 9 KB document, an
empty body) and a `/` in the slug restored exactly into a fresh DB. One
secondary risk: dump files are written in place (export.py:290), not
temp+rename, so a crash can leave a torn file that a later restore reads
(INV-16).

### INV-07: Unapproved text reaches a model only inside the frame
**Rule.** Every title, body, snippet or history entry of a row with
`trusted_at IS NULL` that any surface emits (MCP, HTTP, CLI output an agent
can read, hooks) sits between `UNTRUSTED_OPEN` and `UNTRUSTED_CLOSE`. The
title is inside the frame. That covers every copy of the text: the lexical
index column (`stemmed`) is never emitted, and an error message (a
duplicate refusal) names a slug, never a title. No line of the framed
content, under *any* Unicode line boundary (`str.splitlines()`), begins
(after anything that renders as nothing: whitespace, combining marks (Mn,
Me) and `hooks.renders_as_nothing`, which is every format character (Cf),
every unassigned code point (Cn) and the default-ignorable ones that are
neither, plus the blank Braille cell; *nineteenth review:* U+2065, an
unassigned default-ignorable, let `\u2065>>> END UNTRUSTED MEMORY` through)
with a run of angle brackets or look-alikes that shows three brackets
(`≫` and `«` show two, `⋙` and `⫸` three). *Third review:* a
hand-listed set of format characters let U+061C through. *Fourth review:*
the run was counted in characters, so `≫>` and `⋙` passed. *Fifth
review:* the same invisibles, except whitespace, may sit between the
brackets (`>\u200b>>`, `>\u0301>>`), and the look-alikes include `⧼⧽`,
`≺≻`, `⪝⪞`, `ᐸᐳ` and `⪻⪼` (two). *Seventeenth review:* a hand list
missed a family each time (`⦑⦒`, `⋖⋗`, `⪦⪧`). The class is every character
whose Unicode name says angle bracket, angle quotation mark, less-than,
greater-than, precedes or succeeds, plus `ᐸᐳ`, `˂˃`, `˱˲` and `⨠`
(*twenty-second review* added the last three); one named double
shows two, and triple or very-much shows three. So do `⨠`, drawn `>>`, and
one named two brackets beside or overlapping (`⪥⪤`): *r03 review:* `⨠`
counted one, and `⨠> END UNTRUSTED MEMORY` closed the frame. The rule stays "at the start of a line".
Every control character in the framed content that is neither a tab nor a
line break (`str.splitlines()`) is shown as its escape (`\x1b`); *twenty-first
review:* "nor a space" left U+001F, which `str.isspace()` calls whitespace,
and `>\x1f>>` showed `>>>`. *Twentieth review:* `>\x1b[m>>`
counted one bracket, and click strips ANSI sequences from output that is not
a terminal, so an agent read a clean `>>>` closing line; the reader's view is
the text after its controls are gone.
A history entry's `changed_by` (a caller's `--agent`) is framed like its
reason. So are an unapproved record's `links_out`, words of its body
(*twenty-second review:* `mem_get`, `/get` and `cat --links` listed them
raw); `frame_for_model` frames them with the body. They are the words of the
stored text, so an approved record's links are words the owner approved
(*twenty-fourth review:* every surface took them from the raw body before
`upsert` scrubbed it, and a pack update passed none, so `<private>` text
inside a `[[link]]`, or the links of a replaced version, were served
unframed once the record was approved). `upsert` derives the stored links
from the scrubbed text on every write, and `read_record` serves the words
of the body it serves; no caller passes links. *Seventh review:* `skillmem trust` shows the text unframed and
unescaped on purpose. It prints only past the owner gate (INV-02), so its
reader is the owner by the rule's own signal, and the owner approves exactly
the text shown; escaping a bracket run would show other text than the one
approved.
**Enforced by.** `hooks.frame_for_model` (hooks.py:194) and
`hooks.render_untrusted` (hooks.py:270), the one renderer; `storage.upsert`
and `storage.read_record` for the links.
**Check.**
1. Hypothesis over bodies and titles containing `>>> END UNTRUSTED MEMORY`
   variants after every separator in {`\n`, `\r`, `\r\n`, `\x0b`, `\x0c`,
   `\x1c`–`\x1e`, `\x85`, U+2028, U+2029}, NBSP and fullwidth brackets. For
   each surface's output, split with `splitlines()` and assert that exactly
   one line (the last of the block) starts with a bracket run.
2. For each surface, store an unapproved row whose title and body are
   sentinels. Assert that every occurrence of either sentinel in the output
   lies between the markers.

**Status.** GAP.
- `_BRACKET_RUN` anchors on `(?m)^`, which is `\n` only. A close marker
  after `\r`, U+2028, U+2029, U+0085, VT, FF or FS survives **(probe)**.
  The hook recall path is safe, because `_one_line` collapses whitespace and
  session-history re-joins `splitlines()`. The following are not safe:
  `mem_get`, `mem_search`, `mem_recall`, `/get`, `/search` and `/recall`
  (JSON with `ensure_ascii=False` keeps U+2028/U+2029/U+0085 raw), and
  `skillmem cat`/`recall` text. Fix in `render_untrusted`: normalise every
  `splitlines()` boundary to `\n` before substituting.
- `skillmem search` prints unapproved titles and snippets unframed
  (cli.py:161–163), and `--format json` prints raw rows (cli.py:146). This
  is the twin that `mem_search` covers.
- `skillmem cat` prints the title outside the frame (cli.py:178; the body
  is framed at 194–196).
- Listings (`mem_list` mcp_server.py:149–153, `/list` server.py:306–313,
  `skillmem ls` cli.py:224–227) carry raw unapproved titles. That was a
  stated 0.11.0 exception, but it contradicts the rule and the briefing's own
  reasoning (storage.py:2690–2692). Recommendation: frame the title there
  too.

### INV-08: A write is acknowledged only if it is visible, and a failure is reported as one
**Rule.** A write that returns success (CLI exit 0 "OK", MCP result without
`isError`, HTTP 2xx, importer counted as inserted/updated) is readable
afterwards by `get` with the text written (as `scrub` stored it), and by
the ranked reads (it is not archived or deleted); by its caller, too, on the
surface that acknowledged it (*eighth review:* HTTP hid a `shared` record
from its author when the author held none of its topics). A write that did not
happen returns an error on every surface; an importer command that failed
any file exits non-zero (*third review:* `migrate` and `import-vault`
exited 0 when every file failed; *fourth review:* so did `skills add` with
every skill refused, and `skills-restore` of an unknown slug). A file an
importer leaves out is reported, never passed over (*twenty-second review:*
a symlinked note leading out of the vault was counted nowhere, and a
`SKILL.md` that is not UTF-8 was stored with replacement characters). A
pack's skills under a vendored, built or test folder (`packs.SKIP_DIRS`)
are not the pack's; the name of a skill's own folder never leaves it out
(*twenty-fourth review:* `skills/build/SKILL.md` and `skills/test/SKILL.md`
were left out, unreported, with exit 0). A link
to a file inside the tree is a second name for its text, even if the target's
name is not one the importer discovers (a pack's `procedure.md`, or a vault's
`procedure.txt`). Vault and `migrate` discovery (one function) reads each
file once, known by its device and inode, preferring its own name when
discoverable (*r05 review:* a resolved path keeps the link's spelling on
APFS, so `link.md -> NOTE.md` stored `note.md` twice, and `migrate` read a
link leading out of its directory and passed a directory link over, exit 0);
packs deduplicate by name, title and text.
An unreadable link is reported, and so is a folder or file an importer
cannot list or read: vault, `migrate` (its discovery too) and packs walk
with one function, `migrate.tree` (*r06 review:* rglob and glob pass over a
folder they cannot read, and all three commands exited 0 without its notes;
an unreadable `SKILL.md` aborted the pack with a traceback). *r08 review:*
auto-discovery carries an unreadable projects root into the import failure
report too; a warning alone is not a reported failure (*r13 review:*
`skillmem recap` logged a missing `claude`, a failed model call or a refused
index write to `hooks.log` only, and exited 0 "recap run"; `run_recap`
returns why it wrote nothing, and the command fails with it; *r15 opus
review:* `migrate` with no `--source` and no terminal, which imports nothing
so an old Stop hook cannot import every project, exited 0). An inaccessible parent
is not an absent root (`Path.exists()` hides both on Python 3.14).
Directory symlinks are not traversed and are
reported as not imported, whether their targets are inside or outside the tree;
otherwise skills behind them silently disappear from an apparently complete import.
So is a real subfolder a flat walk (`migrate`) does not search (*r08 opus
review:* its notes were left out with exit 0, while the same folder behind a
link was reported).
One run never writes two sources into one slug.
An unresolved attachment fails its note, whether named by frontmatter or
by a body embed: missing, unreadable, unsupported and out-of-vault targets
are never silently dropped (`vault._collect_attachments`, *r09 review*).
An explicit `attachments` key overrides body embeds, including an empty
list (INV-14); a dump restores only its recorded list.
Migration may skip a copy only when its text, kind, session, origin and named
metadata fields match: an omitted field and an explicit null are different
instructions. *Eighteenth review:* comparing only title and body silently
dropped a second file's different kind or session. The one
exception is a dump restore of a record the dump says is archived: it is
restored archived, readable by slug only (INV-06 requires it). A revived
tombstone comes back active.
An exception raised inside a `storage.tx` block, including `KeyboardInterrupt`,
`SystemExit`, generator close or cancellation, rolls back that scope and
re-raises, one raised as the scope opens too (*r09 opus review:* a Ctrl-C
while `BEGIN` waited for another writer left the transaction open, and every
later write on the connection was a savepoint inside it, acknowledged and lost;
`storage.tx` and `storage.snapshot` open inside their `try`), and one raised
as it closes: a COMMIT that fails or is interrupted rolls back (*r16 opus
review:* COMMIT sat outside the handler, the twin of the r09 fix).
*r18 astra review:* a failed or interrupted savepoint RELEASE is handled
there too, so catching it in the outer scope cannot commit the cancelled
scope's writes and history. A failed savepoint preserves its outer transaction; a failed
outer transaction releases its lock and discards deferred embeddings.
Reusing the connection must not acknowledge writes that disappear on close
(*r06 review:* both rollback handlers caught only `Exception`). A write
inside an explicit outer transaction remains provisional until it commits.
**Enforced by.**
- `storage.tx` rolls back on `BaseException` at both transaction depths.
- `upsert`'s tombstone refusal (storage.py:1548, 1654–1662, `WHERE … OR
  revive` at 1903) and rowcount check (1921–1928).
- `create_only` (1545).
- MCP `_Err` → `isError` (mcp_server.py:65, 665–668).
- HTTP 403/409/422 mapping (server.py:387–393).
- Importer collision refusals (migrate.py:186–199, vault.py:283–290).

**Check.** After every successful surface call in a random sequence, assert
`get(slug).content_hash == hash(written text)`, and that `lifecycle !=
'archived'` and `deleted_at IS NULL`. After every refused call, assert the
error channel was used.
**Status.** GAP. A write over an archived record succeeds and stays
archived (known issue K1; **probe**: `mem_update` returns `ok`, row stays
`lifecycle='archived'`, and search does not find the new text). A same-text
`mem_write` over it also returns `ok`. The recommended behaviour is in
section 4, K1.

### INV-09: No embedding is computed while the write lock is held
**Rule.** The embedding model runs only when the connection is outside a
transaction. A vector is stored only for the `content_hash` it was computed
from.
**Enforced by.** `_vector_ids`, the one query embedding, returns nothing
inside a transaction, so a read in a caller's `tx` ranks by BM25 alone
(*tenth review:* `search` and `recall_skills` embedded the query under a
caller's write lock). `_set_embedding`, the one embedding write, defers when
`conn.in_transaction` (storage.py:1944–1947); `reindex_embeddings` writes
through it (*third review:* it embedded inside a caller's transaction). `tx` flushes after the outermost COMMIT
(storage.py:151–153). A compare-and-set `WHERE content_hash = ?`
(storage.py:1962, 1994). A text change nulls the old vector (1898). Blobs of
the wrong width are skipped (2429, 3188).
**Check.** Replace `embed.embed_text` with a function that tries `BEGIN
IMMEDIATE` on a second connection with `busy_timeout=0`, as the audit test
at line 795 does. Drive every write surface, including HTTP and wrapped
imports. Assert the attempt never failed. After each step, assert
`embedding IS NULL OR embedded_for(content_hash)`.
**Status.** HOLDS. Two notes:
- A SAVEPOINT rollback (a failed file in a vault import) leaves its deferred
  entry queued. The compare-and-set makes that harmless, but the model runs
  for nothing.
- `snapshot()` does not flush deferred work, so no writer may use it.

### INV-10: Hooks never fail the session
**Rule.** For any stdin bytes, any environment and any file-system state,
every `skillmem hook <name>` and `skillmem inject` (the SessionStart hook)
exits 0. They write nothing to stderr except one line naming the problem,
and never a traceback. They finish within their event's timeout: no model
download (embed.py:71, 99) and bounded input (hooks.py:579).
**Enforced by.** It should be one wrapper around the `hook` group and
`inject`. Today each hook guards its own reads (`_read_input` hooks.py:61,
`_connect` try at 586/634, `_safe` 412, inject cli.py:347–351).
**Check.** Hypothesis generates stdin (arbitrary bytes, non-object JSON,
objects with wrong-typed fields, `~user/` and over-long paths). It also
generates files: a non-UTF-8 baseline, transcript lines whose `message` is a
string, list or number, dangling symlinks, an unreadable database. Run each
hook in a subprocess. Assert exit code 0 and no `Traceback` on stderr.
**Status.** GAP (known issue K4), **probe**:
- `mcp-guard` with a non-UTF-8 `mcp-baseline.txt` exits 1 with
  `UnicodeDecodeError` (hooks.py:672).
- `session-history` and `session-recap` with `transcript_path:
  "~nosuchuser/x"` exit 1 with `RuntimeError` (hooks.py:696, 1017).
- `session-recap` with a transcript line whose `message` is a string exits 1
  with `AttributeError` (hooks.py:983).

### INV-11: Case-insensitive and case-sensitive file systems give the same result
**Rule.** Every file name skillmem derives is either injective under
canonical Unicode normalisation plus `casefold()` or refused. That covers
dump files, body files, assets and manifest entries. Every file name skillmem
looks for (a `*.md` to migrate, a pack's `SKILL.md` or licence file, a note's embed, beside the note or
anywhere in the vault) is matched with canonical Unicode normalisation and
casefolding by comparing names,
never by a glob or a path built from the name, whose case rule is the file
system's (*eighteenth review:* `Rule.MD` and `skill.md` were imported on
Windows only; *nineteenth review:* `current_dir / target` and
`root / "LICENSE"` found `pic.png` and `license` on APFS and NTFS only;
*twentieth review:* a listed attachment looked for in the store as
`assets_root.parent / name`). Where several names match, the exact spelling
is taken first (*twenty-first review:* the first in sort order won, and
`Pic.png` sorts before `pic.png`). Pruning never removes a file that the same run wrote under
a case or canonical Unicode variant. The same inputs give the same database
and the same export on APFS/NTFS and ext4.
**Enforced by.**
- export: `export._filename_key` normalises to NFD before and after
  casefolding for allocation (`taken`, which holds the attachments' paths
  before the first dump is named: *r04 review:* a record of kind `assets`
  dumped over `assets/X.md` on APFS/NTFS), ownership (`planned`/`theirs`/
  `attached`) and pruning (`same_name`), with `_same_file` before a prune.
- export: every file an export writes goes through `export._publish`, which
  first renames an entry on disk that is the same file under another
  spelling to the planned name (*r19 opus review:* APFS/NTFS kept `Foo.md`
  when `foo.md` replaced it, and the manifest listed `foo.md`).
- Import: `vault._named` uses the same key for every attachment lookup
  (beside the note, elsewhere in the vault, or in the store), preserving
  exact-spelling priority. Pack skill and licence discovery and asset
  suffixes use it too. Lowercasing alone missed `Σ.png` / `ς.png`.
- Hooks: `_matching_files` uses that key for session-summary and transcript
  discovery; uppercase prefixes and extensions are found too (*r12 review*).
- Body file names hash the exact slug (storage.py:1100).
  `gc_body_files` compares referenced and scanned names with the same
  `_filename_key` under its write lock: a case or canonical Unicode alias
  of a referenced body is never an orphan.
- Asset names are lowercase hex (vault.py:162).
- `_valid_kind` lowercases kind folders (storage.py:1274).
- Import collision refusals (vault.py:283, migrate.py:186).

**Check.** Hypothesis generates slug sets with case and canonical Unicode
variants (including Hangul syllables and decomposed Jamo). Assert that
the planned paths remain distinct after normalisation and casefolding,
every record has its own dump after repeated exports and pruning, and
a canonical alias cannot overwrite another database's reserved dump. Export
twice with case-renamed slugs on a case-sensitive FS, and assert that every live
record's file exists and none was pruned. The same test run on a macOS/
Windows CI runner gives the same file set.
GC must retain referenced bodies after filename normalisation or case changes,
while still collecting old unreferenced files in its own namespace.
**Status.** HOLDS.

### INV-12: Databases sharing an export directory or data directory are isolated
**Rule.**
- An export never overwrites or prunes a file that another database wrote.
  Attachments too: one is published over a file another entry lists only
  when the bytes are the same (hash-named assets are), unless that entry's
  writer is this database (*r04 review:* only planned dumps were checked).
  Every file an export writes is in its manifest entry before it is written
  (*eighteenth review:* a killed export ran no `except`, and left its files
  listed nowhere for another database to overwrite).
  The reservation survives a failed export: an interrupt can arrive after
  rename but before the file enters the in-memory list of completed writes.
  Only a successful publication and prune may narrow the manifest entry.
  A second database's export that would overwrite or prune a file the first
  wrote is refused before any file is written (disjoint records coexist in
  one directory), and two concurrent exports are serialised. Records with
  one slug are not disjoint, whatever their kind folders: a restore of the
  directory knows a record by its slug, so the export that would put a
  second database's record beside another's under that slug is refused
  (*r16 opus review:* both exports succeeded, and a restore replaced A's
  skill with B's note).
- Body-file GC deletes only files in its own database's namespace.
- A database is the file, not its path: one moved elsewhere keeps its body
  files and its export, and a new database at the old path is another one
  (*sixth review:* the namespace and the export key were the path's hash).
  So is one rebuilt from a dump. The file is named by one spelling,
  `storage.file_path`, however the caller spells it (*r16 opus review:*
  `resolve()` keeps the caller's case on APFS, so `M.db` and `m.db` were two
  namespaces for one file). An export adopts a manifest entry holding
  its records only if this database's id at the entry's path gives the
  entry's key (it moved). An entry whose writer's path is gone, or now holds
  another database, was written by one that moved away or was deleted, and
  nothing tells which: only the owner at a terminal takes it over
  (*eighth review:* a newcomer restored from the dump at a moved database's
  old path overwrote its backup).
  Legacy flat `files` and `default` entries go through the same record
  checks. Without a writer path, matching records require the owner at a
  terminal to adopt the entry; unrelated or unproved files stay reserved
  and are never unconditionally pruned. An entry whose key this database's
  id does not give is adopted file by file: the files holding its records
  (or about to be overwritten by them) become its own, and the rest stay
  that entry's reservation (*twenty-fourth review:* the owner's export over
  a 0.11 manifest adopted the whole flat list on one proved file and pruned
  another database's record and an unproved file).
- A copy of a database file under the same data directory is another
  database, wherever it is put, the moved original's old path included
  (*r03 review:* `cp` copied the stored id, so a copy put back at the old
  path took over the moved original's export and GC'd its body files).
  `init_schema` records the file's inode next to the id; a file whose inode
  differs is a copy and `_db_id` derives it another id. A move across file
  systems is a copy too: its export entry then needs the owner, and its body
  files are copied into its new namespace. A copy of a database that no
  0.12.0 opened before the copy was made cannot be told apart. Opening it (`init_schema`) copies each body file it names in
  another namespace into its own, so the original's GC cannot delete a body
  the copy serves (*seventh review:* it did). A copy the original GCs before
  it is first opened is not protected; nothing tells the original about it.
  If isolation cannot complete (including a read-only copy or a failed
  write lock, or a foreign body missing or invalid when isolation reads it),
  opening fails instead of acknowledging an unprotected copy. The copy's
  write lock cannot stop the original's GC: each foreign body must verify
  before it is published, and a failure rolls back all body-path repairs.
  A database whose body files are already its own can still open read-only.
  `uninstall --purge-db` deletes only its own namespace's body files: a
  pre-0.11 name carries none and may be the one a copy's original serves
  (*twenty-third review:* purging the copy deleted it). A database from
  before 0.12.0 (one with a stored empty id or records) keeps its path's
  namespace, the empty one at the default path. A file at any path that no
  skillmem initialised is not it and owns no body files (*r04 review:*
  purge's own connect created one where no database was, and deleted a
  moved pre-0.12 database's body files; *r11 review:* the r04 fix covered
  the default path only, and a `--db` path still did; `_db_namespace`).
- `export_all` writes nothing to the database.
- The pre-v10 upgrade backup is named for the database file and the second
  (*twentieth review:* the second alone, so a second old database upgraded
  in that second got none). It is taken under the migration's write lock,
  once the columns are found missing, so it holds the database before the
  upgrade (*twenty-first review:* taken before the lock, a second opener
  migrated in between and its copy replaced the real backup; INV-05).
  "Before the upgrade" is before every change an open makes: *twenty-second
  review:* the kind, visibility, strength and TTL repairs ran first, and the
  backup held their results. `_migrate` takes it first and runs them under
  its lock.
- Scheduled jobs are per database: installing, removing or refreshing one
  database's jobs leaves another's alone (*twenty-second review:* fixed job
  names made a second database's install replace the first one's backup
  job). A job name carries a digest of its database's path, except for the
  default database (`schedule._database_tag`). So does the weekly export's
  directory, `backups/vault<tag>` (*twenty-third review:* every database's
  job exported into one directory, and the second one's was refused every
  week). Concurrent cron installs and removals also preserve those jobs:
  `schedule._cron_update` holds the same user's `~/.skillmem-cron.lock`
  from the crontab read through replacement, independent of database and
  `SKILLMEM_HOME` overrides (*r05 review:* distinct job names alone still
  let two stale snapshots overwrite each other). This is an OS file lock,
  not a database transaction: the shared state is the user's crontab.
  External crontab editors do not participate in the advisory lock.
  A job names its database the same from any working directory: every
  path it carries is absolute, because `storage.default_data_dir` and
  `default_db_path` return `SKILLMEM_HOME`/`SKILLMEM_DB` made absolute and
  `schedule._job_env` takes its values from them (*r10 review:* a relative
  override was copied into the job, and the weekly backup exported a new
  empty database from the job's working directory).
  The Claude Code hooks `init` installs open the database its MCP entry
  names: a hook runs with Claude Code's environment, not the entry's, so its
  command carries `--db` (*r15 opus review:* after `--db X init
  --claude-code` recaps went to the default database and recall never read
  X). `cli._skillmem_argv` reads a hook past that `--db`, so a later `init`
  repoints it and `uninstall` removes it.

**Enforced by.**
- `export._locked` (export.py:133) and the ownership check
  (`storage._identity` recomputes an entry's key from its path and our id)
  (export.py:220–238).
- `theirs` (260).
- `_db_identity` (storage.py:1055) and `_db_namespace`/`gc_body_files`
  (1065, 1150); `_adopt_body_files`, called by `init_schema`. Both hash the path with `_db_id`, a random id `init_schema`
  stores in a new database's `meta`; one that held records before 0.12.0
  has none and keeps its path-derived names. `_db_id` is the stored id only
  in the file whose inode `init_schema` stored as `db_file`; `export._wrote`
  asks it too.

**Check.** Two databases with disjoint slug sets export into
one directory in random order and concurrently. Assert that the loser
raises before writing and that the winner's files are byte-identical
afterwards. `gc_body_files(A)` never removes B's files.
**Status.** GAP (known issue K5). Two databases restored from one dump share
`(slug, created_at)` and are taken for one database. See section 4.
*Fixed at 0.12.0:* the manifest records each entry's database path, and an
entry is adopted only when that path is gone or is this database. An entry
written before 0.12.0 has no path: matching records and the owner signal
are required to adopt it. Disjoint exports preserve its reservation.

### INV-13: The history chain records every text and lifecycle change once
**Rule.** Every text change, delete and lifecycle transition (into and out
of `stale` included, and a revive of a tombstone by a restore or a pack
re-install) appends exactly one `memory_history` row, whose `old_*` is the version replaced as read
under the lock. The row is chained on the tip read under the same lock, with
`changed_at` monotonic. A no-op (same text, a second delete, a restore of an
active row) appends nothing.
**Enforced by.** At 0.12.0 one appender, `_append_history`, for every
row: a text change and a revive in `upsert`, `soft_delete`, `set_archived`
and `sweep_lifecycle`. Its `old_body` is the verified body file, else the
excerpt. *Third review:* delete and lifecycle rows had their own appenders,
which recorded an intact document's excerpt. At 0.11.3:
- `_upsert_update_tx` (storage.py:1818–1856).
- `soft_delete` (2019–2044).
- `_append_lifecycle_history` (3075) from `set_archived` and
  `sweep_lifecycle`.
- `_chain_clock` (760) and `_backfill_history_chain` under the migration
  lock (356–373).

**Check.** Run random sequences of edits, deletes and archives across two
connections. `verify_history` reports no break. The number of history rows
equals the number of real transitions in the model.
**Status.** HOLDS.

### INV-14: A write changes only the fields its caller names
**Rule.** A named field is applied as given (empty or null clears; an
invalid value is refused with the same error on every surface: an empty
`kind` or `visibility` is invalid, not a default). Visibility has no null
value on any surface: HTTP create requests distinguish omission (default
on insert, preserve on retry) from an explicitly null value (422). A file
names a field by its key, so `project: null` and `attachments: []` clear,
and a key whose value is not one (`ttl_days: tomorrow`, and *tenth
review:* `ttl_days: 1.5`, `tags: {team: ops}`, `agent: [a]`, `pinned:
'false'`, which were coerced to 1, `[]`, a repr and a pin, and *seventeenth
review:* a boolean in a string field, `project: yes`, stored as `True`;
*twenty-second review:* `!!binary` and `!!set`, stored as `b'hi'` and
`{'x'}`: a string field takes a string or a number as written, and nothing
else; *r09 opus review:* a null inside a list, `tags: [ops, null]`, dropped
while the library refuses it, and a `metadata` key that is not a mapping,
dropped with every kind and session it named, `migrate.split_frontmatter`
refusing it for every reader) fails the file; it
does not leave the field named and empty, or apply another value. The same holds on the wire and
in the library (*eleventh review:* HTTP took `ttl_days: true` for 1): a
boolean is not a number, and `upsert` refuses it.
Integer file fields (TTL, timestamps and counters) refuse floats, including
whole-valued floats such as `7.0`; the shared number parser preserves this
distinction for both plain notes and dumps. Quoted integer strings remain
accepted by the file parser.
Over MCP the tool schema accepts null for every field a null clears (the
SDK validates arguments before a handler runs). An omitted title is
preserved by HTTP `/update` and MCP `mem_update`; a named
null clears it (*eighteenth review:* HTTP kept it, and MCP's schema refused
the null before its handler could do the same). An unnamed
field keeps the row's value as read under the write lock, on same text and
on a text change alike. On insert, an unnamed field takes the surface's
documented default, and a field the surface cannot name at all takes it
whatever the item carries (a plain note's `created_at`). A default the
surface derives (a note's top folder as its project, `--kind`) is only that:
it names nothing on an update (*fifteenth review*), and decides nothing
the row keeps: an origin that follows the kind follows the row's kind
(*r08 opus review:* `--kind document` made a stored note `owner`, sealed, and
migrate's `note` fallback made a stored feedback rule `derived`). The kind
used to derive provenance is normalized by `storage._valid_kind`, just as
the stored kind is: accepted case and whitespace variants of `note` remain
`derived` in both importers, including insert defaults (*r18 astra review*).
Ownership fields (agent, origin, trust, seal, pinned,
lifecycle) follow INV-02, INV-03 and section 2, not this rule.
A file's title is named by its key too (`title` or `description` in a note,
`description` in migrate): `""` or null clears it, and the heading or first
line is only what a file without the key gets (*sixteenth review*).
A field is applied when it is named alone, too: a deadline named without a
TTL is the deadline (*sixth review*). A file key with no value for a field
that has no "none" (`strength: null`, `visibility: null`, `metadata.type:
""`, or null counters, `created_at` or `updated_at`) fails the file. Nullable
recency clocks, TTL and deadline still accept null to clear them.
A string field holds the text the file wrote: YAML's reading of a number
or a timestamp keeps it (*twentieth review:* `title: 1.10` was stored as
`1.1`, `project: 12:30` as `750`, `0x1F` as `31`); a number field gets the
value (`migrate._Loader`, which every frontmatter reader parses through).
Packs use the same parser and scalar validation for `name` and `description`;
malformed YAML fails the file instead of becoming a title (*twenty-first
review*: packs still used a separate line parser).
**Enforced by.** `upsert`'s `explicit` (storage.py:1475): same text at
1364–1390 and text change at 1777–1801. Every caller must pass it.
`explicit=None` is the mode that breaks the rule.
**Check.** For each surface, and for each field × {given, empty, omitted} ×
{insert, same text, text change}, assert the result matches the section 2
table's recommended rule. `explicit=None` must not be reachable.
**Status.** GAP. See the CONFLICT cells in section 2 and K2.

### INV-15: Served text is the verified text
**Rule.** A body file is served, verified, exported or approved only if its
bytes hash to the row's `content_hash`. Otherwise every reader serves the
stored excerpt and says so in the text it serves, first, where a trimmed
recall still shows it (`storage.served_body`; a log line reaches no MCP or
HTTP client). Search snippets retain the entire notice before their selected
text, even when the matching word is far into the excerpt. A history entry that
records an excerpt starts with the same notice (*eighteenth review:* it was
served as the whole old text). Body and
dump files, and `write --body-file` and stdin, are read as bytes. A same-text write re-creates a missing body file.
**Enforced by.**
- `verified_body_file` (storage.py:1209), used by `load_body` (1230),
  `mismatched_bodies` (1191) and history (storage.py:1830).
- `is_excerpt(item, body)`: whether the text `load_body` returned is the
  excerpt, decided on that text, for `served_body`, `trust` and export.
  *Third review:* `trust` and export asked a second read of the file, and a
  same-text repair in between left an excerpt unmarked.
- `served_body` for every reader that serves a body: `read_record`,
  `recall_skills` and `search` (whose hits reach `/search`, `skillmem search
  --format json` and the hook's feedback rules).
- `_stage_body_file` (1112), which writes bytes.
- The same-text repair (1394–1404).

**Check.** Hypothesis corrupts, deletes, re-encodes (CRLF/latin-1) or
truncates body files. Assert that every reader returns either the verified
text or the excerpt, `verify` lists the slug, `trust` refuses and export
marks it `truncated`.
**Status.** HOLDS.

### INV-16: Files are published atomically, and secrets stay private
**Rule.** A file another process or a later run reads is written to a
temporary name and renamed into place. Secret files are 0600 from creation.
A stored token is sent only to the repository it was stored for.
*Twenty-second review:* the token file did not say which, and `upgrade
--repo` sent it anywhere; it now holds the repository beside the token, and
a file without one is sent nowhere. A token from the environment
(`SKILLMEM_GITHUB_TOKEN`) or from `gh` is not one skillmem stored. A redirect
carries a token only to the origin (scheme, host, port) it was sent to
(*twenty-third review:* urllib forwarded it to any host; `cli._gh_get`).
**Enforced by.** `_write_secret` (cli.py:34), `_stage_body_file`
(storage.py:1112), `_publish_note` (hooks.py:905), the config writers
(cli.py:619, 964), `upgrade` (cli.py:1697–1731), and `export._publish` for
dump files, the attachments `vault._store_asset` copies in, and scheduler
launchd plists and systemd services and timers. A
content-addressed file is named for the bytes it is written from, read
once, and is taken as present only if its bytes hash to its name, in any
case (`export.intact_asset`, asked by the export, the restore and
`_store_asset`; *fourteenth review:* the export and the restore took any
file under the name; *twenty-first review:* and any `….PNG` twin, as only a
lowercase name was checked).
**Check.** Kill the writer at each write (patch `os.replace` to raise).
Assert the old file is intact and no reader sees a partial file. Stat modes.
**Status.** GAP (low). Dump files are written in place (export.py:290).
*Fixed at 0.12.0:* `export._publish` writes dump files, assets and the
manifest to a scratch name and renames them.

---

## 2. Metadata field policy

One table. Rows are fields; columns are write surfaces. The CLI column is
`write`/`learn`, MCP is `mem_write`/`mem_update`/`mem_learn` and HTTP is
`/write` `/update` `/learn`. "dump" is `import-vault` of a skillmem export
(`metadata.node_type: memory` and `exported_at`; *r07 review:* the marker
alone is also on every Claude Code auto-memory, which then reset what it does
not state). "note" is `import-vault` of any other markdown file, an
auto-memory included. `migrate` also covers `init --migrate-existing` and the recap hook's
self-index (hooks.py:1224).

The cells record 0.11.3 as found; where a rule in section 1 or section 6 says otherwise, those win.
Each cell gives **ins** (the slug is new), **same** (same title and body,
metadata-only; `_upsert_same_text`) and **chg** (text change;
`_upsert_update_tx`).

Notation:
- `v`: the given value is applied.
- `∅`: the caller gave an empty value (`""`, `[]`, JSON `null`).
- `–`: the caller omitted it.
- `keep`: the row's value, read under the lock.
- `clr`: overwritten with NULL/`[]`/the default.
- `n/a`: the surface cannot express the field, which behaves like `–`.
- `✗`: refused.

On MCP/HTTP `/write`, `/learn` and CLI `learn`, a text change is ✗
(`MemoryConflict` "already exists with different text", storage.py:1694).
Only `write --reason/--force`, `mem_update`, `/update` and the importers
change text.

| field | CLI `write` / `learn` | MCP `mem_write` / `mem_update` / `mem_learn` | HTTP `/write` / `/update` / `/learn` | dump | note | migrate | pack |
|---|---|---|---|---|---|---|---|
| **kind** | write: ins `v`, `–`→`note`; same/chg `v` if `--kind` typed, `–`→keep; `∅`→✗ invalid. learn: always `skill`; a slug holding another kind ✗ (storage.py:1749) | write: ins `v`, `–`→`note`; same `v`; **`∅`→`note` and counts as named: CONFLICT C5**. update: truthy→`v`, `∅`/`–`→keep. learn: `skill`, other kind ✗. Sealed kind change ✗ on all | /write: ins `v`, `–`→`note`; same `v` if sent, `–`→keep; `∅`→422. /update: `v`; `∅` and null→422 (*sixth review:* this cell said null→keep); `–`→keep. /learn: `skill`, other kind ✗ | `metadata.type` → `v` on ins/same/chg; `""`/null → ✗ (the file fails); no key → ins `--kind`, keep | `metadata.type`, else `--kind` (default `document`), always applied on same/chg; sealed ✗ without TTY: **CONFLICT C3** | `metadata.type`, else file prefix, else `note`, always applied on same/chg: **CONFLICT C3** | `skill` |
| **title** | required; `--title ""` accepted; change = text change | write/learn: required, **`∅`→✗: CONFLICT C4**. update: truthy→`v`, **`∅`→keep: CONFLICT C4**, `–`→keep | /write, /learn: required, `∅` accepted. /update: **`∅`→sets `""`: CONFLICT C4**; `–`/null→keep | exact `description` (not stripped, vault.py:133–136) | frontmatter title/description/name → heading → first line, stripped; fallback slug | description → heading → first line; fallback slug | `[pack] ` + first sentence of description |
| **body** | `--body`, `--body-file` or stdin; `∅` accepted | write: required, **`∅`→✗: C4**. update: required, `∅`→✗. learn: built from trigger/steps/outcome | /write, /update: `∅` accepted (C4). /learn: built | verbatim, CRLF kept (vault.py:264–269, 74–80) | CRLF→LF, stripped | stripped | `strip()` + provenance block |
| **project** | ins `v`, `–`→NULL, `∅`→`""`; same/chg `v` if typed, `–`→keep | write/learn: ins `v`; same `v` if not null (`∅`→`""`); null/`–`→keep. update: not null→`v`; `–`→keep | /write, /learn: sent→`v` (**null clears: CONFLICT C6**), `–`→keep. /update: not null→`v`, **null→keep (C6)** | given→`v`; omitted→clr (same and chg) | `--project` > frontmatter key → `v` on ins/same/chg, `∅`/null→clr; `–`: ins top folder, else NULL; same/chg keep (*fifteenth review:* the folder was written on same/chg, and `--project ""` was `–`) | n/a. ins NULL; same keep; **chg clr (C2) (probe)** | `pack:<name>` |
| **visibility** | n/a. write: ins `private`; learn: ins `public`; same/chg keep | write: n/a, ins `private`, keep. update: n/a, keep. learn: ins `v`, `–`→`public`; same `v` if not null | /write: ins `v`, `–`→`private`; existing: equal ok, different→409, `–`→keep (server.py:348–354). /update: n/a keep. /learn: ins `–`→`public` | given→`v`; omitted→`private` (export omits private) | frontmatter value, else `private`, **always applied: a same-text import makes a public row private: CONFLICT C1** | always `private`: **same forces private (C1) (probe)**; chg `private` | `public` |
| **tags** | write: n/a, ins `[]`, keep. learn: `--tags a,b`→`v`; `--tags ""`→`[]` named (clears); `–`→keep | write/update/learn: list→`v`, `[]`→clr (named), null/`–`→keep | /write, /learn: sent→`v` (`[]` clears), `–`→keep. /update: not null→`v`, null→keep | given→`v`; omitted→clr | frontmatter → `v`. same: non-empty→`v`, empty→keep. **chg: always written, `[]` clears: C2 / K2** | n/a. same keep; **chg clr (C2 / K2) (probe)** | `["imported","pack:<p>","untrusted-origin"]` |
| **topics** | n/a: ins `[]`, keep | as tags | as tags | as tags | as tags (C2 / K2) | as tags (C2 / K2) (probe) | `[<pack>]` |
| **ttl_days / freshness_until** | write: `--ttl-days N` (1..3650)→`v`, deadline = now+N·86400 (ins/chg; same only if N changed, storage.py:1412–1416); 0→✗; `–`→keep both. learn: n/a | write/learn: int→`v`; null/`–`→keep; 0→✗; cannot clear. update: n/a keep | /write, /learn: int→`v`; **null sent→clears both (C6)**; `–`→keep. /update: n/a keep | `ttl_days` and `freshness_until` restored exactly (not recomputed, storage.py:1405–1411); `freshness_until` is named by its key, null clears it (*seventh review:* beside a TTL it was derived); a dump without the key derives it from `ttl_days`; `ttl_days` omitted→clr both | `ttl_days`→`v`, the deadline derived from it; `freshness_until` n/a, ignored (*fourth review*) | n/a. same keep; **chg clr (C2) (probe)** | n/a. same keep; **chg clr (C2)** |
| **agent** (author; HTTP private access keys on it) | write: `--agent`→`v` on ins/same/chg; `∅`→`""`; `–`→ins NULL, same keep, chg keep (`fresh.agent or item.agent`, storage.py:1787). **Anyone at the CLI can reassign authorship: CONFLICT C7**. learn: n/a | server-stamped `_agent()` on ins; never named, so same keep; update chg keeps the author, a NULL one included (*sixth review:* this cell said it fills a NULL) | ins = token name; existing keeps `existing.agent or name` (server.py:357, 430) | given→`v`; omitted→clr | frontmatter→`v` on same/chg (C7); **chg `–`→clr (C2)** | n/a. same keep; **chg clr (C2) (probe)** | `import:<pack>` (C7) |
| **source_session** | n/a: ins NULL, keep | n/a: keep | n/a: keep | `originSessionId`→`v`; null→clr | `originSessionId`→`v`; same non-None→`v`; **chg `–`→clr (C2)** | `source_session`/`originSessionId`/`sessionId`→`v`; **chg `–`→clr (C2)** | n/a. **chg clr (C2)** |
| **attachments** | n/a: ins `[]`, keep (storage.py:1791–1792) | n/a: keep | n/a: keep | listed files copied to assets and list applied; omitted→clr (same 1388, chg explicit) | `![[x]]` embeds found → `v`; same: none→keep; **chg: none→clr (C2)** | n/a. **chg clr (C2) (probe)** | n/a. **chg clr (C2)** |
| **origin** | TTY→`owner`, else `agent`: ins `v`; same unchanged; chg `v` | `agent`: ins `v`; same unchanged; update chg→`agent` | `agent`; /update chg→`agent` | recorded origin on ins/same/chg; `owner` only with TTY (vault.py:328–330); no key → ins default, keep (same and chg) | frontmatter `metadata.origin` ∈ {agent, imported, derived}, else a note (the row's kind unless the file names one)→`derived`, else `owner`: ins/chg `v`; same only when the file carries `strength:` | claimable value, else a note (the row's kind unless named)→`derived`, else `agent`: ins/chg `v`; same unchanged | `imported` ins/chg; same unchanged |
| **strength** | n/a: ins 1.0, keep | n/a: keep | n/a: keep | `v` on ins/same/chg (restore) | `strength:`→`v` (and makes the import a restore); `–`→keep | n/a: keep | n/a: keep |
| **pinned** | n/a (`skillmem pin` any caller: C8 / K3) | n/a (`mem_pin` any caller: **CONFLICT C8 / K3**) | n/a | `pinned` key→`v` (vault.py:385); absent (pre-0.11 dump)→keep | n/a: keep | n/a: keep | n/a: keep |
| **lifecycle** | n/a: a write over an archived row leaves it archived (**C9 / K1**) | same (C9) (probe) | same (C9) | `archived`→archived with TTY, else reported `skipped_archive`; absent→an archived row is restored active (vault.py:396–401); `stale` not carried | n/a (C9) | n/a (C9) | n/a (C9) |
| **counters** (access/confirmed/failure) | n/a: ins 0, keep | n/a: keep (`mem_reinforce` moves them) | n/a: keep | given→`v` (vault.py:409–416) | parsed but not applied: keep | n/a: keep | n/a: keep |
| **approval** (`trusted_at`) | TTY: ins/same/chg approved (`cli-tty`); no TTY: ins NULL, same keep, chg clr | ins NULL; same keep; chg clr | ins NULL; same keep; chg clr | never carried: ins NULL, same keep, chg clr | ins NULL, same keep, chg clr | same | same |
| **owner_seal** | minted when origin=owner and TTY, **or whenever the item carries `trusted_at`** (INV-02 GAP); never cleared | not minted (a snapshot carrying `trusted_at` re-mints on an already sealed row) | not minted | `owner_seal: true` raises it with TTY (vault.py:402–408) | minted only with TTY (origin owner) | minted only with TTY | never (a sealed row ✗) |

### Conflicts

**C1: visibility on a same-text import.**
- CLI, MCP and HTTP keep visibility unless it is named.
- `migrate` and the plain-note import pass `explicit=None`, so the default
  `"private"` counts as given (storage.py:1364–1366). A same-text re-import
  makes a public or shared record private. **(probe)**: migrate over
  `visibility='public'` → `private`.

*Rule:* keep unless the file names `visibility`. *Why:* re-reading an
unchanged file is not a decision about audience, and it silently revokes
access for HTTP readers.

**C2: unnamed metadata on a text change.**
- CLI, MCP and HTTP keep every unnamed field from the row read under the lock
  (storage.py:1777–1801, the 0.11.3 fix).
- `migrate`, the plain-note import and pack import pass `explicit=None`, so
  every field of the item is written. Project, agent, tags, topics,
  attachments, TTL and deadline, and source_session are cleared.
  **(probe)**: after a migrate text change, `project=None, agent=None,
  tags=[], topics=[], attachments=[], ttl_days=None, freshness_until=None`.
  The CHANGELOG's known issue names only tags and topics.

*Rule:* INV-14. Importers pass `explicit` built from the frontmatter keys
present. A pack names what it owns (`kind, project, agent, visibility, tags,
topics`). *Why:* this is the same defect 0.11.3 fixed for CLI and MCP, and
the fix belongs in one place. Removing `explicit=None` deletes the second
code path in both `_upsert_same_text` and `_upsert_update_tx`.

**C3: kind from a file default.**
- Explicit surfaces keep kind unless named.
- The plain-note import applies `--kind` (default `document`), and migrate
  applies the `note` fallback, on existing rows. A re-import relabels a
  feedback rule to `document`/`note` and drops it out of the briefing. Only
  sealed rows without a TTY are refused (storage.py:1674).

*Rule:* a file names kind only by `metadata.type` (or, for migrate, a
recognised filename prefix). `--kind` and the `note` fallback are insert
defaults.

**C4: empty title or body.**
- MCP refuses `""` for title and body (truthiness, mcp_server.py:179–182),
  and `mem_update` ignores an empty title (232).
- HTTP and CLI accept `""`, and `/update` sets an empty title
  (server.py:418–419).

*Rule:* presence is required, and emptiness is a value (the same rule as
tags `[]`). MCP checks `is None`. *Why:* one rule at the edge, with
validation left to storage, which already refuses the empty slug.

**C5: empty kind.**
- `mem_write` turns `kind: ""` into `note` *and* marks it named
  (mcp_server.py:190, 208), so a same-text retry relabels a skill as a note.
- `mem_update` ignores it.
- HTTP answers 422 and the CLI refuses it.

*Rule:* a named `""` goes to `_valid_kind`, which refuses it on every
surface.

**C6: JSON null.**
- HTTP `/write` and `/learn` treat a sent `null` as named, so it clears
  (`model_fields_set`).
- HTTP `/update` and all of MCP treat `null` as omitted.

*Rule:* on JSON surfaces, key presence names the field, and `null` clears.
MCP computes `explicit` from `k in args`, as HTTP does from
`model_fields_set`. *Why:* this is the only way an MCP client can clear a
TTL or a project. The tool descriptions ("cannot be cleared here") then
change.

**C7: authorship.**
- MCP and HTTP never change an existing row's `agent` ("authorship is not an
  edit's to change", storage.py:1787).
- CLI `write --agent` reassigns it on same text and text changes (made so in
  0.11.3). Any process with Bash can do that, and HTTP private access keys on
  `agent`.
- Plain-note frontmatter reassigns it too. Migrate clears it.

*Rule:* `agent` on an existing row changes only by a dump restore or by the
owner at a terminal (`--agent` without `owner_present()` → ✗ on an existing
row). On insert it is the surface's stamp.

**C8: pinned.** Archive and delete of a sealed row are owner-only, but
`mem_pin`, `skillmem pin` and `set_pinned` let anyone unpin it (K3).

*Rule:* INV-03: `set_pinned` refuses to change the flag of a sealed row
without `owner_present()`.

**C9: lifecycle under a write.** Every surface agrees (the archived state is
left in place), and every surface violates INV-08. See K1.

**The recommended general rule.** INV-14, plus ownership fields governed
separately:
- `agent`, per C7.
- `origin`, which describes the text: set on insert and on a text change,
  and relabelled on same text only by a dump restore. A surface that can
  name it (a dump, the library) sets it only when it does; one that leaves
  it out keeps the row's, on a text change too, and a kept origin mints no
  seal: only an origin the write states does (INV-14; *r17 opus review:*
  a dump without `metadata.origin` restoring changed text wrote `owner`).
- Trust and seal, per INV-02.
- `pinned`/`lifecycle` of sealed rows, per INV-03.

This puts every guard in `upsert`/`set_pinned` and deletes the
`explicit=None` mode. It also removes the surface-specific default handling
behind C1–C6.

---

## 3. Read-then-write inventory

The table is the 0.11.3 tree. At 0.12.0 every storage site marked **no**,
**partial** or **by caller** decides under the lock or by CAS (section 6,
INV-05), `upsert`'s sealed-revive refusal and the kind repair included.

"Same tx?" means: is the value that drives the decision read under the lock
that makes the write?
- **yes**: it is.
- **CAS**: the write re-states the condition in its `WHERE` or pins a hash.
- **by caller**: it holds only because every current caller wraps the call
  in `tx`.
- **no**: it is not.

| site | read | decision | same tx? |
|---|---|---|---|
| storage.py:`_migrate` kind normalisation (304–317) | rows with unnormalised kind | UPDATE to normal form | **no** (autocommit). *Corrected:* not harmless. Re-applying the normalisation is idempotent, but the read/write pair is not atomic: a valid kind another writer set in between is overwritten by the stale row's normal form |
| storage.py:`_migrate` visibility repair (321–333) | one bad row | UPDATE whose WHERE repeats the test | CAS (single statement) |
| storage.py:`_migrate` version gate | `schema_version` | run the versioned steps | yes: `_migrate_versioned` runs in one `tx`, the version re-read under its lock (*sixteenth review:* they ran in autocommit) |
| storage.py:`_migrate_versioned` history `self_hash` | column exists? | add column + build chain | yes (the gate's tx) |
| storage.py:`_migrate_versioned` stem index and backfill | index objects present? rows with empty `stemmed` | build the FTS table, stem each row, `rebuild`, then the triggers | yes (the gate's tx). *Sixteenth review:* a second opener found the table without its triggers and wrote a row no index held, and the backfill replaced a concurrent tag edit's stems |
| storage.py:`_migrate_owner_seal` (553–611) | column and marker | ALTER + backfill | yes (pre-read is only a fast path) |
| storage.py:`_migrate_v10` (614–637) | columns | ALTER + backfill | yes |
| storage.py:`_restem_all` (498–518; at 0.12.0 `restem_all`) | every row | UPDATE stems | yes |
| storage.py:`gc_body_files` (1150–1188) | referenced `body_path`s | unlink orphans | yes |
| storage.py:`set_trust` (1300–1342) | hash, lifecycle, deletion | approve/revoke | yes (the CLI's display read at cli.py:415 is pinned by `expect_hash`: CAS) |
| storage.py:`upsert`, pre-read (1541–1567) | existing row | create_only, tombstone refusal, sealed-revive refusal, branch, kind pre-check | **partial**. Re-checked under lock: branch (1641–1653), tombstone (1654–1662, 1903), kind (1674, 1807), keep_sealed_text (1664, 1803), create_only (UNIQUE, 1620). **Not re-checked: sealed-revive (1555–1567), by caller** (vault, packs hold `tx`) |
| storage.py:`upsert` conflict scan (1572–1577) | FTS duplicates | refuse a near-duplicate | no, by design (advisory; HTTP's write and learn run it inside their tx, MCP and the CLI before it) |
| storage.py:`_upsert_same_text` (1345) | row re-read at 1642 | metadata diff | yes |
| storage.py:`upsert_skill` (1742–1755) | kind of the slug | refuse a non-skill | yes |
| storage.py:`_upsert_update_tx` (1765–1930) | row re-read at 1771 | unnamed fields, seal, kind, history, approval | yes |
| storage.py:`_set_embedding` (1933–1966) | none (hash passed in) | store vector | CAS (`content_hash`) |
| storage.py:`reindex_embeddings` (1969–1999) | all rows | store vectors | CAS (`content_hash`) |
| storage.py:`soft_delete` (2002–2057) | row, re-read at 2014 | seal, already-deleted, history | yes |
| storage.py:`find_conflicts` (2569–2655) | FTS walk, then title/body by id (2635) | 409 content | read-only; the refetch lacks `deleted_at IS NULL` (INV-04) |
| storage.py:`reinforce` (2821–2864) | kind, lifecycle, deletion, visibility | bump | yes |
| storage.py:`set_pinned` (2902–2933) | row | set flag | yes (no seal check: INV-03) |
| storage.py:`decay_stale` (2936–2977) | idle, unpinned, unsealed skills | decay | yes |
| storage.py:`sweep_lifecycle` (3000–3058) | idle, unpinned, unsealed skills | stale/archive + history | yes |
| storage.py:`set_archived` (3093–3152) | seal, pin, lifecycle | archive/restore + history | yes |
| storage.py:`recall_skills` auto-reinforce (3276–3296) | ranked rows | which to bump | yes (`reinforce` re-checks liveness, kind and, *since the second review*, the caller's `visible` under its lock; before, only the first two) |
| mcp_server.py:`_tool_write` (178–214) | via `upsert` | via `upsert` | inherits `upsert` (partial) |
| mcp_server.py:`_tool_update` (219–252) | `S.get` snapshot | written back | yes (tx at 226) |
| mcp_server.py:`_tool_learn` (255–295) | via `upsert_skill` | kind | yes |
| server.py:`/write` (366–395) | `_gate_create` row | permission, create_only, visibility | yes (tx at 370) |
| server.py:`/update` (397–445) | `S.get` | visibility, `_may_write` | yes (tx at 400) |
| server.py:`/learn` (447–479) | `_gate_create` + kind | permission, kind | yes (tx at 456) |
| server.py:`/recall` (481–510), `/reinforce` (512–525) | visibility | bump | yes (inside `reinforce`) |
| server.py:`/get` (274–298) | row, links, history | response | read-only, `snapshot` |
| cli.py:`write` (248–294) | via `upsert` | via `upsert` | inherits `upsert` (partial) |
| cli.py:`trust` (393–450) | row shown to the owner | approve | CAS (`expect_hash`) |
| cli.py:`learn` (2066–2100) | via `upsert_skill` | kind | yes |
| migrate.py:`import_dir` (236–266) | per-file `existed` (203) and `claimed` | reason, collision | yes (tx at 250, and `import_file` takes its own). *Nineteenth review:* from `run_recap` (hooks.py:1224), `existed` was read outside a tx, and a record created in between got the wrong history reason and was reported inserted |
| vault.py:`_run_import` (254–422) | `existed` (339), `was_pinned` (368), `current` (397) | reason, pin, lifecycle | yes (tx at 248, savepoint at 262) |
| packs.py:`import_pack` (274–287) | `prior` origin/project | ownership | yes |
| packs.py:`remove_pack` (335–357) | unsealed pack rows | soft-delete | yes |
| export.py:`_export_locked` (156–269) | manifest, DB rows (108), `ours` (222) | ownership, prune | manifest under `flock` (133); the two DB reads are separate statements (low) |
| hooks.py:`run_recap` (1011–1227) | debounce stamp, note basis | run, publish, index | per-session O_EXCL lock (1083–1116), the debounce stamp read again under it (*r13 review:* the pre-lock read was the only one, so a Stop that passed it while another ran start to finish made a second model call within `RECAP_MIN_INTERVAL`); publish CAS under file lock (`_publish_note`), and the record indexed under the same lock. *Seventeenth review:* it was indexed after the lock, so a Stop recap that lost the file to SessionEnd wrote its text over the record last |
| hooks.py:`_acquire_recap_slot` (856–887) | stale lock mtime | reclaim | re-stat before unlink (acknowledged window) |
| hooks.py:`auto_recall`/`_append_seen` (570–573, 152–158) | seen-ledger | reset/append | *Corrected:* not a read-then-write. The reset reads nothing: it is the boundary between two prompt cycles, and a line appended before it belongs to the cycle it ends. What must hold is that a line appended after the reset survives, which the truncate-then-`O_APPEND` pair guarantees |
| cli.py config patchers (`_patch_settings_hook` 1154, `_patch_claude_json` 722, …) | config JSON/TOML | rewrite | **no** lock against a concurrent editor (atomic replace only; outside the DB). *r13 review (P3, open):* nor against a second `skillmem` patcher; the CAS window lets one of two simultaneous patches be lost. Known issue |

---

## 4. Deferred known issues (CHANGELOG 0.11.3)

**K1. A write over an archived record succeeds and stays archived.**
Violates INV-08 (and INV-04 from the reader's side). **(probe)**:
`mem_update` returns `ok`, and search does not find the new text.

*Recommended:* **refuse** with `MemoryConflict("'<slug>' is archived; restore
it first (skillmem skills-restore <slug>)")`. Put the check next to the
tombstone checks in `upsert` under the lock (the `sealed_now` re-read at
storage.py:1641–1662 and `_upsert_update_tx`'s `WHERE` at 1903). Every
surface then reports it through its existing error mapping. Refuse, not
restore, because:
1. Archiving is the owner's act. A write that silently un-archives lets any
   agent undo it with one call, which is exactly the reason archiving became
   owner-only in 0.11.1.
2. A restore has side effects a write must not carry: strength is floored
   at 0.5 and recency refreshed (storage.py:3146–3150).
3. It matches `set_trust`, which already refuses archived rows
   (storage.py:1318), and the tombstone rule. That makes one rule: "a write
   lands only on a live, visible row."
4. It is one condition in the place every write passes through.

The dump restore is the only caller that carries lifecycle. It should apply
`set_archived(False)` *before* `upsert` (move vault.py:396–401 up), so the
row is visible when written. A pack update over an archived skill is then
reported under `skipped`.

**K2. `migrate` and a plain-note `import-vault` over changed text write the
file's tags and topics over the row's.** Violates INV-14. It is wider than
stated **(probe)**:
- The same path also clears project, agent, attachments, TTL/deadline and
  source_session, and pack import does the same (C2).
- A same-text import forces `visibility='private'` (C1).
- A same-text import relabels kind from a default (C3).

*Recommended:* every importer passes `explicit` built from the keys its file
states, and `explicit=None` is removed from `upsert`.

**K3. `mem_pin` from an agent can unpin the owner's pinned skill.**
Violates INV-03 (and C8). `skillmem pin --off` without a TTY does the same
(cli.py:2232).

*Recommended:* `set_pinned` refuses to change `pinned` on a sealed row unless
`owner_present()`, checked inside its existing transaction
(storage.py:2911). One guard covers MCP, the CLI and the vault restore
(which runs at a terminal). Pinning an unsealed row stays open.

**K4. Hooks print a traceback (exit 1) on unexpected input.** Violates
INV-10. **(probe)**: the three inputs in INV-10.

*Recommended:* one guard, not one per site. `hook_group` becomes a
`click.Group` subclass whose `invoke` catches `Exception`, appends
`error <hook> <type>` to `hooks.log` (`_log_line`) and exits 0. `inject` is
wrapped the same way; its try at cli.py:347–351 then goes away. Add the
property test from INV-10 to keep it closed.

**K5. Two databases restored from one dump and exported into one directory
are taken for one database.** Violates INV-12. The ownership proof
`(slug, created_at)` (export.py:222–237) is identical for both.

*Recommended:* store the resolved database path next to each manifest
entry. Adopt another entry's files only if that path no longer exists (the
database was moved or rebuilt) or `samefile`s ours. Otherwise refuse, as for
any other database. Two live copies then differ by a path that still exists.
A moved database still adopts its old entry. Export still writes nothing to
the database. The path is one machine's: two machines syncing one export
directory, with the same database path on each, are not told apart.

---

## 5. Gaps found while writing this spec (not in the CHANGELOG)

| # | invariant | where | evidence |
|---|---|---|---|
| G1 | INV-07 | hooks.py:266–280 | close marker after `\r`, U+2028, U+2029, U+0085, VT, FF, FS is not escaped **(probe)** |
| G2 | INV-07 | cli.py:146, 161–163, 178 | `skillmem search` (text and JSON) and the `cat` title are unframed for unapproved rows |
| G3 | INV-07 | mcp_server.py:149–153, server.py:306–313, cli.py:224–227 | listings carry raw unapproved titles (a stated 0.11.0 exception) |
| G4 | INV-01/02 | storage.py:1438, 1606, 1612–1613, 1864–1867; 1300; 1478 | caller's `trusted_at` approves and seals without TTY **(probe)**; `set_trust` has no owner check; `owner_call` is a caller flag |
| G5 | INV-05 | storage.py:1555–1567 | sealed-revive refusal on the pre-lock read (latent; fixed: `_refuse` now reads the row under `upsert`'s lock) |
| G6 | INV-03 | cli.py:2232, 2344; storage.py:3146 | `pin`, `skills-restore` change a sealed row with no TTY |
| G7 | INV-06 | vault.py:112–114, export.py:72 | `updated_at` exported, not restored **(probe)** |
| G8 | INV-16 | export.py:290 | dump files written in place |
| G9 | INV-04 | storage.py:2635 | `find_conflicts` refetch without the deletion filter |
| G10 | INV-04 | mcp_server.py:124–137, cli.py:171–213 | by-slug reads not in one snapshot |
| G11 | INV-02 | vault.py:328–330 + migrate.py:165 | a dump claiming `origin: owner` without a TTY falls through to the importer default `owner` for non-note kinds (no seal is minted; the CLI requires a TTY, so this is library-only) |

---

## Appendix A: audit tests → invariants

Each test in `tests/test_release_0_11_3_audit.py` was red on 3c85541.

- **INV-01, INV-15:**
  - `test_an_intact_crlf_document_is_served_whole`
  - `test_rewriting_the_same_text_restores_a_lost_body_file`
  - `test_a_same_text_write_without_kind_repairs_a_missing_document_file`
  - `test_an_over_long_slug_externalises_its_body`
- **INV-02:**
  - `test_cli_owner_check_is_the_storage_one`
  - `test_migrate_without_source_imports_nothing_from_a_hook`
- **INV-03:**
  - `test_a_pack_update_does_not_rewrite_a_skill_the_owner_approved`
  - `test_a_pack_update_is_refused_under_the_lock_once_sealed`
  - `test_a_same_text_pack_reimport_leaves_a_sealed_row_as_the_owner_left_it`
- **INV-04:**
  - `test_briefing_does_not_count_archived_rows_as_unapproved`
  - `test_search_does_not_return_a_record_archived_after_it_was_ranked`
  - `test_no_ranked_read_returns_a_record_archived_after_ranking`
  - `test_tool_recall_skips_what_the_session_saw_before_the_limit`
  - `test_http_get_reads_history_under_the_same_lock_as_its_check`
- **INV-05:**
  - `test_create_only_refuses_a_row_created_after_the_permission_check`
  - `test_an_http_update_is_authorised_under_the_write_lock`
  - `test_reinforce_asks_visibility_under_the_write_lock`
  - `test_recall_reinforces_only_what_is_still_visible`
  - `test_mcp_update_reads_and_writes_under_one_lock`
  - `test_mcp_learn_checks_the_kind_under_the_write_lock`
  - `test_a_pack_import_checks_ownership_under_the_write_lock`
  - `test_a_same_text_update_compares_metadata_under_the_write_lock`
  - `test_a_reindex_does_not_index_text_an_edit_replaced`
- **INV-06:**
  - `test_a_crlf_body_survives_export_and_restore`
  - `test_restoring_a_dump_over_the_same_text_restores_empty_fields`
  - `test_restoring_a_dump_over_the_same_text_restores_its_deadline`
  - `test_restoring_an_unchanged_backup_keeps_a_padded_title`
  - `test_an_export_carries_a_notes_attachments_to_the_restore`
  - `test_a_dump_restore_keeps_the_backups_session`
  - `test_a_same_text_dump_restore_brings_back_session_and_attachments`
  - `test_a_text_changing_dump_restore_clears_attachments_the_backup_lacked`
  - `test_an_over_long_slug_does_not_abort_the_export`
  - `test_export_from_a_read_only_database`
- **INV-07:**
  - `test_no_near_copy_of_the_close_marker_survives_the_frame`
  - `test_skills_add_frames_the_pack_licence`
  - `test_the_frame_keeps_ordinary_unicode_and_catches_lookalike_brackets`
  - `test_the_frame_leaves_brackets_inside_a_line_and_catches_them_at_its_start`
- **INV-08:**
  - `test_a_failed_mcp_tool_call_says_isError`
  - `test_an_empty_slug_is_refused`
  - `test_a_slug_with_a_slash_is_addressable_over_http`
  - `test_migrating_every_project_does_not_write_one_over_another`
  - `test_a_vault_import_does_not_let_two_notes_share_a_slug`
  - `test_two_files_in_one_directory_with_one_slug_are_refused_not_overwritten`
  - `test_same_body_different_titles_are_not_skipped_as_a_copy`
- **INV-09:**
  - `test_a_text_change_drops_the_vector_of_the_old_text`
  - `test_a_late_embedding_does_not_land_on_newer_text`
  - `test_no_embedding_is_computed_under_the_write_lock`
  - `test_an_http_write_does_not_hold_other_writers_during_the_embedding`
  - `test_a_blob_of_the_wrong_width_does_not_break_vector_search`
- **INV-10:**
  - `test_hooks_never_download_the_model`
  - `test_tool_recall_ignores_a_non_object_tool_input`
  - `test_session_recap_skips_a_transcript_line_that_is_not_an_object`
  - `test_the_session_start_inject_hook_survives_an_unusable_database`
- **INV-11:**
  - `test_slugs_differing_only_in_case_get_two_files`
  - `test_a_second_export_keeps_every_case_variant`
  - `test_a_renamed_record_is_dumped_under_its_new_spelling`
- **INV-12:**
  - `test_a_second_database_cannot_overwrite_the_first_ones_export`
  - `test_a_moved_database_still_exports_into_its_own_directory`
  - `test_a_refused_export_writes_nothing`
  - `test_two_databases_exporting_at_once_cannot_both_win`
  - `test_a_database_rebuilt_from_its_dump_exports_into_that_directory`
  - `test_a_moved_database_whose_records_were_all_deleted_prunes_its_export`
  - `test_an_export_that_fails_halfway_lists_what_it_wrote`
  - `test_scheduled_jobs_name_the_install_database_from_any_cwd`
- **INV-13:**
  - `test_an_interrupted_history_migration_does_not_brick_the_db`
  - `test_a_v10_history_with_every_hash_cleared_is_not_re_signed`
  - `test_a_late_opener_does_not_rebuild_a_chain_another_opener_built`
- **INV-14:**
  - `test_cli_write_of_the_same_text_applies_agent`
  - `test_a_cli_text_edit_keeps_what_no_option_names`
  - `test_an_mcp_text_edit_keeps_the_author`
  - `test_learn_refuses_a_slug_that_holds_a_note_on_every_surface`
- **INV-16:**
  - `test_regenerated_tokens_file_is_owner_only`
  - `test_a_failed_token_write_leaves_the_old_file_alone`
  - `test_regenerating_tokens_through_a_symlink_replaces_the_target`
  - `test_upgrade_sends_no_stored_token_to_the_public_repo`
- **No data invariant (scheduler quoting):**
  - `test_cron_line_runs_with_space_percent_and_dollar_in_the_data_dir`


---

## 6. Status at 0.12.0

The property tests in `tests/properties/` check sections 1 and 3 with
hypothesis and with real second writers. The mapping to invariants is in the
file headers. At 0.12.0 they all pass, together with the audit tests in
Appendix A.

| invariant | 0.12.0 | where |
|---|---|---|
| INV-01, INV-02 | HOLDS | `upsert`/`_resolve` stamp approval and the seal from `owner_present()`; `set_trust` asks it too. `owner_call` and caller `trusted_at` are gone. *Eighteenth review:* `trust` showed the text raw, so control characters hid part of what was approved; `_as_seen` escapes them. *Nineteenth review:* it left variation selectors and fillers raw, and did not pin the kind it showed; it escapes everything `renders_as_nothing` names, and `set_trust` compares the kind under its lock *Twenty-second review:* a same-text kind change kept approval, so the owner's routine `migrate` of an agent's relabelled file made an approved note a rule; `_resolve` clears approval on a kind change unless the owner at a terminal is writing it. *r04 review:* an open's kind repair (`Feedback` to `feedback`) kept approval and made an approved note an injected rule; `_heal` clears it in the repairing `UPDATE`. *r06 review:* the owner's `write` without `--kind` over an agent's record approved the agent's kind; `_resolve` approves only a kind the write names or the owner approved. |
| INV-03 | HOLDS | one check in `upsert` on the row read under the lock; `set_pinned`, `set_archived` and `soft_delete` check the seal. *Review:* the sweep staled sealed rows and `reinforce(failure)` walked their strength below tool-recall's floor; both now spare a sealed row without the owner. *Second review:* decay did the same over eight fortnights; it spares a sealed row as it spares a pinned one. *Third review:* `reinforce` and `set_pinned` asked the TTY, so an HTTP server in the owner's terminal let agents lower and unpin sealed rules; every mutation now asks `_owner(surface)`. A strength above `STRENGTH_CAP` is refused, so a `self_report` no longer caps a rule down. *Fifth review:* a repair (body file name, lexical index) moved a sealed row's `updated_at`; a repair now moves it on no row (decided in the rule: `updated_at` is not a listed field). *Eighth review:* `uninstall --purge-db` deleted the database file, sealed rows and body files with it, with no terminal; it passes `cli._owner_only` and has a deny rule. *r04 review:* with no usable state directory the seen ledger fell back to the shared temp directory, where a pre-created file hid an approved rule; the fallback is gone |
| INV-04 | HOLDS | `_fetch_live(ids, visible, **filters)` re-applies the caller's predicate and the ranking's `_rank_filter` (kind, project, kind exclusion; *review:* a kind changed between ranking and fetch passed an exclusion) to the row whose text it returns. `find_conflicts` refetches live rows only and re-applies its filter. `read_record` is the one by-slug read (row, body, links, history in one snapshot) behind `cat`, `mem_get` and `/get` |
| INV-05 | HOLDS, one residual window | Storage: every site in section 3 decides under the lock or by CAS. The migration's kind backfill re-states what it read in its `WHERE`. *Sixteenth review:* the versioned migration steps ran in autocommit, so a second opener wrote between the FTS table and its triggers (a row no index held, whose next update SQLite called a malformed database) and the stem backfill overwrote a concurrent tag edit's stems; they run in one `tx`, the version re-read under its lock, and the backfill stems what `upsert` stems. *Second review:* recall's auto-reinforce asks the caller's predicate under the lock (`reinforce_retrieved`, shared by `recall_skills` and HTTP `/recall`). Hooks: the recap slots, the per-session recap lock and the publish lock are OS locks (`hooks._try_lock`), released by the kernel when the holder dies; the stale-file reclaim (stat, then unlink) is gone; the debounce stamp is read again under the per-session lock (*r13 review*). Config patchers: `_patches_config` makes every read-modify-write a compare-and-swap that re-runs on a concurrent save. An editor takes no lock we could share, so a save between the final compare and the rename can still be lost; so can a second `skillmem` patcher's (*r13 review*, P3, Known issue). *Fourteenth review:* the compare now follows the scratch write, next to the rename; the window is scoped out of the concurrency class in section 7 and listed under Known issues. *Seventeenth review:* a recap indexed its note into the database after the publish lock; it now does so under it *Twentieth review:* the export's body read raced a GC; an excerpt is read again under the write lock. *Twenty-third review:* a busy lock fell back to the stale row; only a read-only database does. *r11 review:* the export's dumped records and its ownership proof were two reads; they are one statement. *Twenty-fourth review (P3, open):* processes opening a brand-new database at once fail with "database is locked" at `PRAGMA journal_mode = WAL`, which does not wait on the busy timeout (setting the timeout first does not help); no write is lost or acknowledged, and it is a Known issue. |
| INV-06 | HOLDS | a dump restore brings back `updated_at`. *Review:* a counters-only restore stamped the current time over it, and un-archiving after the write floored strength at 0.5; the restore now keeps a named age and un-archives before the write (section 4, K1). *Second review:* an empty `project`, `agent` or `source_session` came back NULL; a write now stores `""` as NULL. The rule is per restored record (see its correction). *Third review:* a dump restores `created_at` too. *Sixth review:* and `last_accessed_at` and `last_decayed_at`; the round-trip property compares every field. *Seventh review:* a deadline cleared beside a TTL came back derived; export writes `freshness_until` null too and a named null clears it. The property moves every field it compares off its default, and a meta-test names them; `confidence` and `supersedes_id` are gone from `MemoryItem`. *Tenth review:* a named time is the time, 0 included; a fresh restore replaced `created_at: 0` with the current time. The property writes epoch-0 times. *Fourteenth review:* a lost body's dump was looked for only under the record's new file name, and a damaged attachment replaced its backup; see the rule *Twentieth review:* a strength above 2.0 (0.11) or out of range, and a TTL out of range (0.10), was exported and refused on restore; `_migrate` brings them into range on open. *Twenty-first review:* a kind `_valid_kind` refuses (0.10 stored any) was left for the owner and refused on restore; `_migrate` repairs it on open (`storage._repaired_kind`). An edit that changed the kind or deleted the record, with a GC, turned the dump into an excerpt; the export plans from the row re-read under the lock. *Twenty-third review:* a slug of dots was dumped under a name the restore does not read, and a dump's `owner_seal`, `origin` and `lifecycle` were taken truthy or defaulted; the name drops leading dots, and an invalid state fails the file. *Twenty-fourth review:* a restore into a fresh database gave a 0.11-format hash the new format; that is the rule's intent (a legacy hash admits an alias), and the rule now says so. Approval survives a restore only with the same kind too (INV-01), which the rule did not name. *r09 opus review:* skipping auto-memories skipped every dump; an auto-memory has no `exported_at`. *r11 review:* a `""` project, agent or session 0.11.3 stored came back NULL; the open stores it as NULL. *r12 opus review:* a note restored over a same-text document kept its body file and `get().body` was the excerpt; the kind and length alone place a body, the round-trip property compares `body` and rekinds the record between export and restore, and schema v12 moves each body an earlier version placed otherwise. |
| INV-07 | HOLDS | `render_untrusted` escapes a bracket run after every `splitlines()` boundary and any leading invisible format characters, and the class includes the mathematical, small-form, ornament and modifier-letter angle brackets. `skillmem search` (text and JSON), the `cat` title, every listing (`ls`, `skills-top`, `mem_list`, `/list`) and history reasons are framed. *Review:* a duplicate refusal quoted the other row's title and a search hit carried `stemmed`; neither does now. *Third review:* the invisible prefix is every Cf, Mn and Me character, not a list. *Fourth review:* a run is counted in the brackets it shows, so `≫>`, `»>`, `⋙` and `⫸` are escaped. *Fifth review:* invisibles between the brackets no longer end the run, five more look-alike pairs are in the class, and a history row's `changed_by` is framed. *Seventh review:* the `trust` preview is the owner's and stays verbatim (see the rule). *Seventeenth review:* the class is derived from Unicode names (`hooks._brackets`), not listed; `⦑⦒`, `⋖⋗` and `⪦⪧` were missing *Twentieth review:* `>\x1b[m>>` passed as one bracket and click stripped the sequence for a non-TTY reader; `render_untrusted` shows every non-space control as its escape. *Twenty-first review:* U+001F is a space to `isspace()` and breaks no line; every control but a tab and a line break is escaped. *Twenty-second review:* `links_out` of an unapproved record was emitted unframed (listed below since the sixth review); `frame_for_model` frames it. `˱˲⨠` join the look-alike class. *r04 review:* `⊀⊁⋠⋡` are named DOES NOT PRECEDE/SUCCEED and were missed; the name pattern takes the singular. *Twenty-fourth review:* the stored links were the caller's, taken from the raw body, so `<private>` text in a `[[link]]` outlived the scrub and a pack update kept a replaced version's links, served unframed once approved; `upsert` derives them from the scrubbed text and `read_record` from the body it serves. |
| INV-08 | HOLDS | a write over an archived record is refused (K1). *Review:* a revive skipped that check and left a pack skill archived; a revived tombstone now comes back active. *Third review:* `migrate` and `import-vault` exit 1 when a file failed. *Fourth review:* `skills add` exits 1 when a skill was refused, and `skills-restore` is `skills-archive --restore`, which exits 1 on an unknown slug. *Fifth review:* two skills whose names slugify alike are no longer merged; the second is reported skipped. K1's "one call": `skills-restore` un-archived any unsealed record with no terminal and had no deny rule; every owner-only verb (`trust`, `rm`, `skills-archive` both ways, `skills rm`, `import-vault`) now passes one gate, `cli._owner_only`, and `skills-restore` has a deny rule. *Eighth review:* HTTP `/write` acknowledged a `shared` record its author then could not read, list, search or update (none of its topics were the author's); `_visible_to` lets the author see its own `shared` rows. *Ninth review:* two SKILL.md files with one name and different procedures were taken for per-agent copies and the second dropped with exit 0; a copy is now the same name and the same text, anything else is reported skipped. *Eleventh review:* a SKILL.md over the per-file cap, or past the pack's aggregate caps, was left out unreported with exit 0; each is reported skipped. *Twelfth review:* the ninth review's "same text" compared name and procedure, not the title the description gives; a copy is now the same name, title and procedure. Frontmatter that did not parse was read as none and the file imported under its file name with success; it fails the file. *Fourteenth review:* `init --migrate-existing` exited 0 when a file failed; it exits 1. *Sixteenth review:* a pre-v5 database's upgrade died on "database disk image is malformed": the backfill's update trigger deleted from the new, empty index rows it never held. The index is now built with `rebuild` after the backfill, and its triggers created last. *Eighteenth review:* `write --body-file` (and stdin on Windows) read in text mode stored LF for CRLF and said OK; both read bytes *Twenty-second review:* `import-vault` counted a symlinked note leading out of the vault nowhere and exited 0, and `skills add` stored a non-UTF-8 `SKILL.md` with replacement characters; both are reported (`migrate.iter_notes`, `packs.iter_skill_files`, a strict decode). *Twenty-fourth review:* `skills add` left out a skill whose own folder is named like a skipped one (`build`, `test`, `dist`), unreported with exit 0; `packs.SKIP_DIRS` applies to the folders above it only. *r05 review:* `import-vault` stored one note twice through a link naming it in another case or Unicode form (APFS), and `migrate` read a link out of its directory and passed a directory link over; both discover through `migrate.iter_notes`, which knows a file by its inode. *r06 review:* a folder `import-vault`, `migrate` or `skills add` could not read was passed over with exit 0; `migrate.tree`, their one walk, reports it. *r08 opus review:* `migrate`'s flat walk passed a real subfolder over with exit 0; `iter_notes` reports it. *r09 opus review:* a Ctrl-C as `tx()` or `snapshot()` opened left the transaction open; they open inside their `try`. |
| INV-09 | HOLDS | *third review:* `reindex_embeddings` writes through `_set_embedding`, which defers inside a caller's transaction. *Tenth review:* `search` and `recall_skills` embedded the query inside a caller's transaction; `_vector_ids` ranks by BM25 alone there |
| INV-11 | HOLDS | *Eighteenth review:* `migrate`, pack import and a note's embed search globbed, so a name spelt in another case was found on Windows only; they compare lowered names, as `import-vault` compares suffixes. *Nineteenth review:* the embed beside the note and a pack's licence file were looked up by path; they are compared by name too *Twentieth review:* the store lookup for a listed attachment built a path; it compares names. *Twenty-first review:* a case twin, first in sort order, won over the exact spelling for an attachment and a licence file; the exact name is taken first (`vault._named`). *T7 review:* export uses canonical Unicode normalisation with casefolding at every filename comparison; NFC/NFD aliases no longer overwrite records or another database's backup, or prune a freshly written file. *r04 review:* the dump names left out attachments, so a record of kind `assets` replaced `assets/X.md` on APFS/NTFS only; allocation starts from the attachments' names. *r19 opus review:* a replaced dump kept the old file's spelling on APFS/NTFS (`Foo.md` for the listed `foo.md`); `_publish` renames it to the planned name first. |
| INV-10 | HOLDS | one fail-open boundary around every hook and `inject` (K4). *Eighth review:* `--db` naming a directory was refused by the group's option parser, before that boundary, with exit 2; `--db` no longer checks the path (*corrected at the tenth review:* it still checked readability, click.Path's default, so an unreadable database was exit 2; it now checks nothing), and the other commands report a database they cannot open in one line. *Ninth review:* the group resolved `--db` before the boundary, so a symlink loop was a traceback; a path that does not resolve is left as given. `inject` alone did not switch stdout to UTF-8 (the `hook` group's callback did), so on a cp1252 pipe its briefing failed to encode and came out empty; the boundary does it. "One line" covered `sqlite3.Error` only; an `OSError` (a parent that cannot be made) is one line too. *Tenth review:* an unknown `~user` in `--db` raised `RuntimeError` in the group, where the fallback expanded it again; the group leaves such a path as given and `connect` expands it inside the boundary. The hook matrix runs every database state both from `SKILLMEM_DB` and from `--db` |
| INV-12 | HOLDS | K5 fixed: the manifest records each entry's database path. *Fourth review:* the rule said every second database was refused; only one that would overwrite or prune the first's files is. *Sixth review:* a new database at a moved one's old path took its namespace and export key, GC'd its bodies and pruned its export; both now include the database's own id. *Seventh review:* a copy under the same home named the original's body files, and the original's GC deleted them; opening a copy files its own. Which body files are a database's is decided in one place, `storage._file_namespace`, for `gc_body_files`, `_adopt_body_files` and `uninstall --purge-db`. *Eighth review:* a database restored from another's dump at its old path (or anywhere, once it moved away) adopted the moved one's export and overwrote its backup; an entry is adopted only by the database whose id gives its key, or by the owner at a terminal (a rebuilt database's weekly export then runs again). *Ninth review:* the moved database itself was refused once a new one sat at its old path: the live-copy check asked whether that path was another file, not whether its id gives the entry's key (`export._wrote`). *Twelfth review:* ownership was proved from a file's frontmatter, and a listed file whose frontmatter named no record was skipped, so another database overwrote it; an existing file another entry lists and that proves nothing refuses the export unless the entry's readable files prove it ours. *Eighteenth review:* the manifest was saved after the files or in an `except`, which a signal or power loss skips; it lists every planned file before the first write *Twentieth review:* the pre-v10 backup is named for the database too, and renamed into place. *Twenty-first review:* it was taken before the migration's lock, and a second opener's migrated copy replaced it; it is taken under the lock, through a second connection. *Twenty-second review:* `schedule install` for a second database replaced the first one's jobs (they are named per database now), and the pre-v10 backup was taken after the open's repairs (it is taken first, under the lock the repairs run under). *Twenty-third review:* `uninstall --purge-db` also deleted the pre-0.11 body files it referenced, which a copy shares; it deletes its namespace only. Every database's weekly export went to one directory; each has its own. *Twenty-fourth review:* the owner's export over a 0.11 manifest adopted the whole flat entry on one proved file and pruned another database's record and an unproved file; an entry whose key our id does not give is adopted file by file. *r04 review:* attachments skipped the ownership check and a second database's replaced the first's listed file; one is published over another entry's file only with the same bytes. `uninstall --purge-db` with nothing at the default path took the empty file its connect made for the pre-0.12 default database and deleted a moved one's body files; the empty namespace needs a stored empty id or records. *r10 review:* a relative `SKILLMEM_DB`/`SKILLMEM_HOME` was copied into scheduled jobs, which run from another directory and backed up a new empty database; the overrides are made absolute where they are read. *r11 review:* the r04 purge fix covered the default path only; a file no skillmem initialised owns no body files at any path. |
| INV-13 | HOLDS | *review:* the sweep's move into `stale` now appends its row. *Second review:* a same-text revive (dump restore, pack re-install) appends "restored from deleted". *Third review:* one appender records the verified text for deletes and lifecycle moves too |
| INV-14 | HOLDS | `explicit=None` is gone; each surface names only what its input states (K2). *Review:* the note and migrate importers named a field by its value, not its key, so `project: null` kept the old project. *Third review:* `attachments: []` in a note, and a JSON null over MCP (refused by the tool schema), did not clear. *Fourth review:* a file key with an invalid value was dropped and left named, so the import cleared the TTL or reset strength; it now fails the file. `_valid_visibility` took `""` for `private`. On insert a plain note set `created_at` and `freshness_until`, which its surface cannot name. HTTP `/write` and `/learn` refused a null `tags`/`topics`. *Sixth review:* `freshness_until` named without `ttl_days` was dropped; `metadata.type: ""` gave the default kind in both importers (`migrate._file_kind` now answers for both); `strength: null` and `visibility: null` reset to the default. *Eighth review:* a UTF-8 byte order mark hid the frontmatter from every importer (stored as the body, `\ufeff---` as the title); files are read as `utf-8-sig`. *Tenth review:* importers coerced what they should refuse: a mapping as `tags`, `topics` or `attachments` became `[]`, `ttl_days: 1.5` became 1, `true` a number, a mapping `agent` or `project` its repr, `pinned: 'false'` a pin; each fails the file. *Eleventh review:* the rule held for files only: HTTP `/write` and `/learn` coerced `ttl_days: true` to 1 and the library stored `1.5`; `upsert` refuses a TTL that is not an `int` (a `bool` is not one) and a `bool` strength, and the HTTP models are strict. `tags: [true]` became the tag "True" and `project: 0` no project; the first fails the file, the second is `"0"`. *Twelfth review:* the library stored a mapping `tags`, `topics` or `attachments` as its keys and `project=0` as a number; `upsert` refuses a list field that is not a list of strings and a `project`, `agent` or `source_session` that is not a string. In a note, a body embed put back the attachments `attachments: []` cleared; the key names the list, and embeds stand in only without it. `pinned: null` in a dump unpinned; it fails the file. Frontmatter that is not a YAML mapping was read as none; it fails the file (`migrate.split_frontmatter`, shared by both importers). *Sixteenth review:* `title: ""` or `description: ""` (null too) was replaced by the heading or the name; the key names the title, and a mapping or list title fails the file. *Seventeenth review:* a YAML boolean in a string field (`project: yes`, `metadata.type: on`) was stored as `True`; `_scalar` refuses it, and the name and kind go through `_scalar` too, so a mapping or list `name` fails the file. `migrate` read `source_session: 0` as absent while naming the field; the first session key given is the value. *Eighteenth review:* `tags: ""` in a note stored the tag `""`; a list key's `""` is empty *Twentieth review:* YAML numbers and timestamps in string fields keep the text written. *Twenty-second review:* `_scalar` refused a listed set of types and stored the repr of the rest (`!!binary`, `!!set`); it takes a string or a number and refuses everything else. *r07 review:* `import-vault` took a Claude Code auto-memory (`metadata.node_type: memory`, no `exported_at`) for a dump, which names every field, and reset its visibility, tags, topics, project, TTL and strength; only a file with both keys is a dump (`vault._is_dump`). Its recorded origin, absent, also became the importer's `owner` on same text; as a note the row keeps it (the seal still follows INV-02 and section 2: the owner's import at a terminal mints it). *r08 opus review:* on a text change the origin followed the importer's default kind, not the row's (`--kind document` made a stored note `owner`); `migrate._origin_from` reads the row's kind in the write's transaction for both importers. *r09 opus review:* a note's `tags: [ops, null]` dropped the null the library refuses, and a `metadata` key that is not a mapping was dropped; both fail the file. |
| INV-15 | HOLDS | *review:* `served_body` puts a notice before an excerpt served in place of the text; `read_record` and `recall_skills` serve through it. *Second review:* `search` did not, and its hits carry `body`; it does now. *Third review:* export and `trust` decide "excerpt" on the text they read (`is_excerpt`). *Eighteenth review:* history recorded an excerpt without the notice; `_append_history` adds it |
| INV-16 | HOLDS | *Twentieth review:* scheduler launchd plists and systemd services and timers now use `export._publish` too; a failed refresh preserves the old complete file. The earlier HOLDS label was wrong while these files were written in place. G8 fixed: dump files, assets and the manifest are written to a scratch name and renamed. *Second review:* an attachment imported by `import-vault` was copied in place, and a retry kept a torn copy because its name existed; it is published the same way and re-copied unless its bytes hash to its name. *Fourth review:* the asset was hashed and copied in two reads, so a save in between filed new bytes under the old hash; it is read once. *Fourteenth review:* export and restore took a stored attachment by its name alone; both ask `intact_asset` *Twenty-first review:* which checked a lowercase name only, so a damaged `….PNG` twin was restored; it checks a hash name in any case. *Twenty-second review:* the stored GitHub token went to any repository `upgrade` named; the token file names its repository and the token goes there alone. *Twenty-third review:* a redirect to another host carried it; it keeps it only to the same origin. |

Not changed, listed for later: slugs are caller text emitted unframed
(a slug may carry a newline and a close marker), and so are `project`,
tags, topics, `agent` and `source_session` (INV-07 names only titles,
bodies, snippets and history); a slug is stored with its surrounding
whitespace, so `write --slug "x "` makes a record `cat x` cannot find;
`find_conflicts` also matches archived rows; `init --migrate-existing`
reported importer failures in its JSON and still exited 0 (*fixed at the
fourteenth review*, see INV-08). *Sixth review:* `links_out`,
taken from an unapproved body's wikilinks, is emitted unframed too (INV-07
does not list it; *fixed at the twenty-second review*); `cat --history` shows no old title or body; `inject`
calls a note sealed by `import-vault` at a terminal "rewritten since you
approved" it.
*Eighth review:* C0 controls (ESC, NUL) before a bracket run are not
skipped by `render_untrusted`, so an ANSI-prefixed close marker survives at
line start (INV-07 names Cf, Mn, Me and whitespace only); a text-changing
revive appends one row labelled with the import's reason, not "restored from
deleted"; after a dump restore gives a pack skill another author, every
re-install of that pack refuses it; `cat --history` prints `by=` with the
framed author on the lines below. *Ninth review:* the hooks' fail-open
boundary writes no `hooks.log` line, so `hooks-status` cannot tell a hook that
failed from one never run; `skillmem search` hides `kind='note'`, `write`'s
default, by default; `find_conflicts` sends an arbitrary 32 of a text's words
to FTS (the set varies with hash seed); `▷ ▹ ► ➤ ⊳` are not in INV-07's look-alike
class. *Eighteenth review:* `trust` pins the hash it showed, not the
kind, so a kind changed before the owner answers is approved with it; a
restore of a deleted record its dump says is archived appends two history
rows; a strength an agent lowered before the seal carries into the owner's
sealed rewrite; a stored GitHub token is not tied to a repository and
urllib forwards it across a redirect (*both fixed, at the twenty-second and
twenty-third reviews*); export compared names with casefold
alone (*fixed at T7 review:* canonical Unicode aliases now share one
comparison for allocation, ownership and pruning; NFC and NFD can be one
name on APFS). *Twenty-third review:* `migrate`'s `note` fallback kind,
an insert default, decides the origin of an existing record on a text
change (a `feedback` record becomes `derived`; *fixed at the r08 opus review*,
with its `import-vault --kind` twin); MCP normalises a visibility
(`" Public "`) that HTTP refuses with 422; rotating `hooks.log` loses a
line another hook appends during the rotation. *r04 review:* an
agent-chosen `kind` (up to 32 characters, no newline) is shown unframed
like the slug; a hook serving rows whose body files are missing writes a
stderr line per row, not one (INV-10); the library `upsert` takes a bool,
float or string time or counter, and a float or string one fails the dump's
restore (INV-14); a library-written `.txt` or extension-less attachment
fails the dump's restore (INV-06). Unreported outside-vault and unsupported
embeds (`![[demo.mp4]]`, *r04/r08 reviews*) are fixed at r09: they fail the
note like every unresolved attachment (INV-08). *r08 opus review:*
a library-written attachment list with a duplicate entry loses the
duplicate in a round trip (INV-08, INV-06).

## 7. Release bar

A release requires **zero P1 and zero P2** findings in three classes:

- **data**: data loss. A record, a field, a history row, a body file or a
  backup is lost, overwritten or silently not written.
- **trust**: approval, trust and sealing. Unapproved text passes as approved,
  a caller without the owner signal approves, seals or changes a sealed
  record, or unverified text is served as the verified text.
- **concurrency**: a read-then-write decision made outside the write lock
  (outside the write's `storage.tx()`, or without a compare-and-set).

Every other finding, of any severity, may ship only if it is listed under
"Known issues" in that release's CHANGELOG section with its INV id. A finding
that is in none of these three classes, or is P3 in one of them, and is not
listed there blocks the release too.

A finding takes the class of the invariant it violates. When it violates
several, it takes the strictest class among them: a stale read (INV-04) that
puts unapproved text in front of a model is also INV-07, so it is **trust**.

| invariant | class |
|---|---|
| INV-01 Approval is bound to the content hash | trust |
| INV-02 Only the owner mints trust or a seal | trust |
| INV-03 A sealed record changes only by the owner | trust |
| INV-04 Reads return live rows as they are at read time | other |
| INV-05 A read-then-write decision is made in the write's transaction | concurrency |
| INV-06 Export → import-vault → export round-trips | data |
| INV-07 Unapproved text reaches a model only inside the frame | trust |
| INV-08 A write is acknowledged only if it is visible, and a failure is reported as one | data |
| INV-09 No embedding is computed while the write lock is held | other |
| INV-10 Hooks never fail the session | other |
| INV-11 Case-insensitive and case-sensitive file systems give the same result | data |
| INV-12 Databases sharing an export directory or data directory are isolated | data |
| INV-13 The history chain records every text and lifecycle change once | data |
| INV-14 A write changes only the fields its caller names | data |
| INV-15 Served text is the verified text | trust |
| INV-16 Files are published atomically, and secrets stay private | data |

INV-04 and INV-09 are about what a reader sees and how long the lock is held.
Neither is a read-then-write, so by themselves they are **other**. INV-05 on a
file another program writes and takes no lock for (an editor's config file)
is **other** too: no process can decide and rename such a file under one
lock, and the compare-and-swap next to the rename is as close as a file
system allows (*fourteenth review*). It must still be listed. INV-16's
second half, a secret readable by another user, is **trust**. A regression test
for a fix must fail on the parent of the commit that adds it
(`scripts/release-gate.sh` check 5).
