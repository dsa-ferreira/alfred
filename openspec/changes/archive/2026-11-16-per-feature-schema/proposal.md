# Proposal

## Why

`db/models.py` holds every table of every feature, together with their
status vocabularies, as one horizontal layer. After `slice-layout`, each
feature owns its code but not its data. The schema also couples slices the
code no longer does:

- `queue_items` carries reminder columns;
- `entries` points at `reminders` and `queue_items`;
- `attachments` points at both `queue_items` and `entries`;
- `conversations` carries the summary state of `archive` and the extraction
  state of `facts`.

Two writes also cross slices: `Queue.enqueue_user` inserts attachment rows,
and `Reminders.claim_due` inserts queue items.

alfred is still in early, single-owner use, and the owner accepts wiping
the database. That makes this the cheapest moment to give each slice its
own tables, rather than carrying migrations for a shape we're about to
throw away.

Second of three changes: it follows `slice-layout` and comes before
`turn-kinds`.

## What Changes

- **Tables per feature.** Each slice defines its tables in its own
  `tables.py` on a shared `db.Base`. `alfred/schema.py` (entry rank)
  imports them all for Alembic. `db/models.py` is deleted.
- **Slice-shaped schema.** A table references only its own slice's tables
  or tables of slices ranked below it:
  - `queue_items` stores each kind's data in a `payload` JSONB column. The
    reminder columns and the unused `telegram_message_id` are dropped, and
    the stored kind `user` becomes `chat`.
  - `entries` replaces the reminder columns with an `origin` JSONB column
    and keeps the turn's id without a foreign key. Attachments are linked
    through a new `entry_attachments` table owned by `conversations`.
  - `attachments` loses `queue_item_id`, `entry_id` and `position`. A chat
    turn's payload lists its attachment ids, in order.
  - Summary state moves to `conversation_summaries` (`archive`), and
    extraction state to `fact_extractions` (`facts`).
- **Unit of work.** A slice writes only its own tables. Writes that span
  slices run in one transaction opened by the rank above, which calls each
  slice's session-taking operation: queuing a chat message with its files,
  and claiming due reminders into the queue.
- **BREAKING: database wipe.** Migrations `0001`-`0010` are replaced by one
  baseline. A database at an old revision is refused by `alfred migrate`
  with a message saying to drop it. Conversations, facts, reminders and
  attachments are lost. Orphaned bucket files are removed by the existing
  orphan sweeper.
- **Compatibility code removed.** This covers reading checkpoints written
  by 0.16.0, the due-time fallback for items queued before migration 0008,
  the `user`/`chat` kind mapping, and the `ALFRED_MIGRATE_ADMIN` path.
- Version 0.17.0, with a handover update for the wipe.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. alfred behaves the same on an empty database; the wipe is a
deployment step, not a requirement change. The change sets
`skip_specs: true`.

## Impact

- Code: every slice gets a `tables.py`. Changed: `turns/queue.py`,
  `turns/kinds.py`, `turns/inputs/`, `conversations/store.py`,
  `attachments/` (intake, tools, handles), `archive/` (summaries, sweeper
  job, search), `facts/` (extraction job), `reminders/store.py`,
  `agent/inline.py`, `startup.py`, `migrations/`.
- Tests: `tests/db/test_schema.py` becomes per-feature table tests. Tests
  of the old migrations' upgrades and downgrades are deleted with those
  migrations, which is the one place this change alters what tests assert.
- Deployment: **the database must be dropped and recreated** (handover
  update 0.17.0). `ALFRED_MIGRATE_ADMIN` is no longer read. Rolling back
  to 0.16.2 needs a `pg_dump` taken before the upgrade.
- Docs: AGENTS.md gets an "Adding a table" section. README's data section
  and handover follow the new tables.
