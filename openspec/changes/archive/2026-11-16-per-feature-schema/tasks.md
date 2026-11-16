# Tasks

Every group ends with `make lint` and `make test` green. Group 1 is a
move-only commit (added to `.git-blame-ignore-revs`).

## 1. Tables per feature (same schema)

- [x] 1.1 Add `db/base.py` (`Base`, naming convention, `in_values`), and move each table with its status vocabulary to its slice's `tables.py` (design D1, same columns as today); verify `test_models_match_migrations` still passes against the existing migrations
- [x] 1.2 Add `alfred/schema.py` importing every `tables.py`, point `migrations/env.py` and `startup.warn_unknown_users` at it, delete `db/models.py`, and add `alfred.schema` to the entry row of the import-linter contract; verify `make lint` passes and `grep -rn "db.models" src migrations` finds nothing

## 2. Unit of work (same schema)

- [x] 2.1 Add `attachments.add(session, user, files)` and `Queue.add(session, user, ...)`/`Queue.notify()`, and move the chat enqueue into `turns/intake.py` (design D5), which implements `attachments.intake`'s `Enqueue` protocol; verify the intake tests pass, including the one where a failing insert leaves neither the item nor its files
- [x] 2.2 Make `Reminders.claim_due` take a session and return occurrences, and move the due loop to `turns/due_reminders.py`, which queues them in the same transaction; delete `reminders/loop.py`; verify the due-reminder tests pass, including order by due time and one item per recipient
- [x] 2.3 Add `conversations.append_recall` and use it from archive search in place of inserting `entries` directly; verify the recall tests pass
- [x] 2.4 Make the agent record tool calls through a `CallLog` protocol implemented in `turns` (design D3); verify `agent/` writes no table
- [x] 2.5 Check rule D2.1 across `src/alfred`: every `session.add(`, `insert(`, `update(` and `delete(` of a table appears only in its own slice; verify with a grep per `tables.py` class, listed in the task's commit message

## 3. Slice-shaped schema and baseline

- [x] 3.1 Delete migrations `0001`-`0010`, and add `s001_baseline.py` generated from `schema.metadata` and edited by hand (vector check, generated column, partial and expression indexes; design D6); verify `test_models_match_migrations` and an upgrade-downgrade-upgrade test of the baseline pass
- [x] 3.2 Make `migrate.upgrade` refuse a database at an unknown revision with design D6's message, before touching anything; verify with a test that stamps `0010` and expects `MigrationError` and an untouched `alembic_version`
- [x] 3.3 Give `queue_items` the `payload` column and drop `source`, `text`, `telegram_message_id`, `reminder_id` and `reminder_due_at`; add `turns/payloads.py` (design D4) and make the queue, kinds and input requests read payloads; fill `ReminderPayload`'s snapshot in `turns/due_reminders.py` and delete `ReminderLookup`; store kind `chat` and delete `_STORED_CHAT` and the `turn_kind` mapping; verify payload round-trip tests pass, the reminder-turn tests pass without a reminder store, and `grep -rn '"user"' src/alfred/turns` finds no queue kind
- [x] 3.4 Reshape `entries` (`turn_id` without a foreign key, `origin` JSONB in place of the reminder columns) and add `entry_attachments`; drop `queue_item_id`, `entry_id` and `position` from `attachments`, with `ChatPayload.attachment_ids` giving the order; update the conversation store, transcript, agent messages, inline attachments and attachment handles; verify the history, transcript, inline-attachment and attachment-tool tests pass
- [x] 3.5 Move summary state to `conversation_summaries` and extraction state to `fact_extractions` (design D3), with the archive and facts jobs finding pending work by left join; verify the sweeper, summary, search and extraction tests pass, including oldest-first order and retry backoff
- [x] 3.6 Move the constraint tests to `tests/<slice>/test_tables.py` and delete the per-migration tests (design D8); verify `tests/db/` holds only engine and migrate tests

## 4. Compatibility removed

- [x] 4.1 Remove the code in design D7: `Checkpoint`'s 0.16.0 read path, the reminder due-time fallback, and comments about old rows; verify `grep -rnE "0\.16\.[0-9]|before migration|before 000[0-9]|MIGRATE_ADMIN" src/alfred` finds nothing, and the checkpoint test keeps only the current shape

## 5. Release

- [x] 5.1 Add "Adding a table" to AGENTS.md: tables in the slice's `tables.py`, registered in `alfred/schema.py`, design D2's three rules, and session-taking operations for cross-slice writes; verify every path it names exists
- [x] 5.2 Update README's data section for the new tables and the refusal of old databases; verify it no longer mentions `ALFRED_MIGRATE_ADMIN`
- [x] 5.3 Bump the version to 0.17.0 in `pyproject.toml`; verify `make image` would tag `alfred:0.17.0`
- [x] 5.4 Write the 0.17.0 handover update with the `homelab-handover` skill: the wipe steps and rollback from design's Migration Plan, the removed `ALFRED_MIGRATE_ADMIN`, and a verify line for a fresh first message; verify the update file exists under `handover/updates/` and its `before:` lists the dump and the drop
