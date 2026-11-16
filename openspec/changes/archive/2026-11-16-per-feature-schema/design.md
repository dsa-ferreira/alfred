# Design

## Context

See proposal.md for why. This assumes `slice-layout` is done: features
are packages in a declared order (its design D1), and `db/models.py` is the
last horizontal module.

The current schema's cross-slice links:

```
queue_items.reminder_id            -> reminders          (turns -> reminders)
entries.queue_item_id              -> queue_items        (conversations -> turns: up)
entries.reminder_id                -> reminders          (conversations -> reminders: across)
attachments.queue_item_id          -> queue_items        (attachments -> turns: up)
attachments.entry_id               -> entries            (attachments -> conversations: up)
conversations.title, summary, ...  (archive's state on conversations' table)
conversations.facts_outcome, ...   (facts' state on conversations' table)
turn_tool_calls.queue_item_id      -> queue_items        (written by agent, which ranks below turns)
```

Writes across slices: `Queue.enqueue_user` (attachments, attachment
pages), `Reminders.claim_due` (queue items), `History.append` (sets
`attachments.entry_id`), and `ConversationSearch` (inserts `recall`
entries).

`tests/db/test_schema.py` has 28 tests. Fourteen of them exercise
particular migrations: upgrades and downgrades, legacy rows,
`ALFRED_MIGRATE_ADMIN`. `test_models_match_migrations` compares the models
with the migrated schema.

## Goals / Non-Goals

**Goals:**

- Each table is defined in, and written only by, the slice that owns it.
- No foreign key points up or across the feature order.
- One baseline migration, and a clear refusal of old databases.

**Non-Goals:**

- No change in behaviour on a fresh database.
- No keeping of old data, and no data migration.
- Turn kinds keep their current shape; this change only gives them
  payloads to read. `turn-kinds` moves the payload types into the kinds.

## Decisions

### D1. Where tables live

- `db/base.py` (infra): `Base`, the naming convention and the `in_values`
  check helper.
- Each slice's `tables.py` holds its tables and their status vocabularies.
- `alfred/schema.py` (entry rank) imports every `tables.py` and exposes
  `metadata`. `migrations/env.py` and `startup.warn_unknown_users` use it.
  It is added to the entry row of the import-linter contract.

### D2. Schema rules

1. **Writes:** a slice inserts, updates and deletes only its own tables.
2. **Reads:** a slice may read, and join, tables of slices ranked below it.
   Joins stay in SQL where they matter for speed (archive search, the
   window query); going through the lower slice's API for every read would
   cost queries for no decoupling gain.
3. **Foreign keys** point only within the slice or down the order. A link
   that would point up becomes either a plain column with no foreign key
   (an idempotency key) or a table owned by the higher slice.

### D3. The tables

```
attachments    attachments (id, user_name, kind, mime, file_name, size_bytes,
                            object_key, width, height, page_count, created_at)
               attachment_pages (attachment_id, page, text)

conversations  conversations (id, user_name, started_at, ended_at,
                              end_reason, marker_sent_at)
               entries (id, conversation_id, kind, source, text,
                        turn_id,                 -- no FK; unique (turn_id, kind)
                        recalled_conversation_id, origin JSONB, created_at)
               entry_attachments (entry_id FK, attachment_id FK, position)

archive        conversation_summaries (conversation_id PK FK, title, summary,
                        summarised_at, embedding, embedding_model,
                        attempts, next_at, search_tsv GENERATED)

facts          facts (... conversation_id FK ON DELETE SET NULL ...)
               fact_extractions (conversation_id PK FK, outcome, attempts, next_at)

reminders      reminders, reminder_recipients            (unchanged)

turns          queue_items (id, kind IN ('chat','ask','reminder'), user_name,
                            payload JSONB, status, attempts, next_attempt_at,
                            failure_reason, created_at, finished_at)
               turn_tool_calls (queue_item_id FK, ...)
               input_requests  (queue_item_id FK, ...)   (unchanged)
```

- **Pending summary and extraction work.** An archived conversation with
  no `conversation_summaries` row, or with one whose `next_at` has passed or
  whose embedding model is stale, is pending for `archive`. One with no
  finished `fact_extractions` row is pending for `facts`. The sweeper jobs
  find these with a left join on `conversations` (rule 2). Each job writes
  its own row (rule 1), so archiving a conversation writes nothing for
  them.
- **`turn_tool_calls`** belongs to `turns`. The agent records calls
  through a `CallLog` protocol that `turns` implements, because the agent
  ranks below `turns`.
- **`entries.origin`** is a small typed value owned by `conversations`
  (`EntryOrigin(reminder_id, due_at)`), stored as JSON and read back by the
  window query and the transcript. It records what arrived, so it has no
  foreign key to `reminders`.
- **`entries.turn_id`** keeps today's job (a retried turn records its
  entries once) without depending on `turns`. The parked-window query
  already receives its hidden item ids as values (`slice-layout` D5).

### D4. Queue payloads

- `turns/payloads.py`: frozen `ChatPayload(text, attachment_ids)`,
  `AskPayload(source, prompt)` and `ReminderPayload(reminder_id, due_at,
  text, created_by, repeat_label, quiet_ok)`, each with `to_json` /
  `from_json` and a round-trip test.
- **The reminder payload is a snapshot taken when it fires.** A reminder's
  text, creator, rule and quietness never change after it's set, so the
  snapshot equals what the kind reads today through `ReminderLookup`. The
  lookup, and its "reminder not found" branch, go away. This also fixes
  the payload's JSON shape now, so `turn-kinds` can move the types without
  stranding queued or parked items.
- `Queue.add(session, user, payload) -> int` takes the kind from the
  payload's type. The queue's read model hands out a typed payload, and
  kinds read it instead of ORM columns.
- `Queue.notify()` wakes the worker after the caller's transaction
  commits.
- The stored kind is `chat`, and the `user`/`chat` mapping goes.

`turn-kinds` moves each payload type into its kind module. Until then
they sit together, so this change doesn't need to restructure the kinds.

### D5. Unit of work

Session-taking operations, each in the slice that owns the table:

```
attachments.add(session, user, files) -> list[int]
reminders.claim_due(session, now) -> list[Occurrence]
queue.add(session, user, payload) -> int
conversations.append(..., attachment_ids=...)        (links entry_attachments itself)
conversations.append_recall(session | own tx, ...)   (archive no longer inserts entries)
```

The orchestrators live in `turns` (rank 4), above every slice they
combine:

```
turns/intake.py          cancel the user's parked requests, then
                         begin: ids = attachments.add(...); queue.add(ChatPayload(text, ids))
                         notify
                         (implements attachments.intake's Enqueue protocol)

turns/due_reminders.py   every check:  begin: for occ in reminders.claim_due(...):
                                                 queue.add(ReminderPayload(...))
                         notify          (replaces reminders/loop.py)
```

Both stay atomic: a message never exists without its files, and a claimed
reminder is never lost.

Alternative considered: a generic `UnitOfWork` object handed to every
slice. Rejected: two call sites don't justify a framework, and a plain
`async with sessions.begin()` reads more clearly.

### D6. Baseline migration and the wipe

- Delete `migrations/versions/0001`-`0010`. Add `s001_baseline.py`,
  generated from `schema.metadata` with autogenerate and then edited by
  hand. Keep:
  - the check that fails clearly without the `vector` extension;
  - the generated `search_tsv` column;
  - partial and unique indexes;
  - the `to_tsvector` expression index.
- The id `s001` can't be confused with an old revision. Until 0.17.0 ships,
  the baseline is edited in place rather than stacked with new revisions.
- `migrate.upgrade` reads `alembic_version` first. A revision the script
  directory doesn't know raises `MigrationError`: "this database was
  created by alfred 0.16 or older and can't be upgraded; drop and recreate
  it (see the 0.17.0 handover update)". It never touches the old tables.
- Bucket files of the old database have no rows left. `OrphanSweeper`
  deletes them after a day, as it does any orphan.

### D7. Removed compatibility

| Code | Why it existed |
|---|---|
| `Checkpoint.from_json`'s 0.16.0 shape | turns parked by 0.16.0 |
| `due_at or reminder.due_at` in the reminder kind | items queued before migration 0008 |
| `_STORED_CHAT`, `turn_kind` mapping | queue rows from before kinds |
| `ALFRED_MIGRATE_ADMIN` mentions, migration 0005's admin assignment | single-user data |
| comments about rows "from before 0006" and similar | old rows |

The backfill behaviour in the specs stays. Archived conversations without
a summary or extraction are still processed oldest first; that's just how
the jobs find work.

### D8. Tests

- Constraint tests move from `tests/db/test_schema.py` to each slice's
  `tests/<slice>/test_tables.py`.
- Kept: `test_models_match_migrations` (against `schema.metadata`), the
  missing-vector test, and an upgrade-downgrade-upgrade test of the
  baseline.
- Added: refusal of an old revision, and payload and origin round trips.
- Deleted with their migrations: per-revision upgrade and downgrade tests,
  legacy-row tests and `ALFRED_MIGRATE_ADMIN` tests.

## Risks / Trade-offs

- [Data loss is real] → the owner accepted it. The handover update says to
  `pg_dump` first, which is also the only way back to 0.16.2.
- [JSONB payloads lose foreign keys and NOT NULLs] → the kind is the only
  reader and writer, and each payload has a round-trip test. Reminders are
  never deleted, only change status.
- [Left joins to find pending work could get slow] → a handful of users
  and thousands of conversations at most. The joins use primary keys, and
  partial indexes can be added if the sweeper's query log says so.
- [`entries.turn_id` without a foreign key could hold a bad id] → it is
  written only from a running turn's own id, and it's used only for
  idempotency and the parked-window filter.
- [Rule 2 (reads may join lower slices) weakens isolation] → it is
  one-way, matching the import order, and writes stay strictly per slice.

## Migration Plan

1. Back up: `pg_dump` the `alfred` database (only needed for rollback).
2. Stop alfred.
3. Drop and recreate the database, or `DROP SCHEMA public CASCADE; CREATE
   SCHEMA public;`. Make sure the `vector` extension exists, created by a
   superuser as on first install.
4. Deploy 0.17.0. `alfred migrate` creates the baseline on start.
5. Remove `ALFRED_MIGRATE_ADMIN` from the environment if it's set.

Rollback: stop alfred, restore the dump into an empty database, and pin
0.16.2.
