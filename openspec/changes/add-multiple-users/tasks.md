# Tasks

## 1. Users in config

- [x] 1.1 Add the `[[users]]` section to `config.py` (name, telegram_user_id, address, admin, tools, mcp_tokens), build `User` values, and remove `[telegram] owner_user_id` and `[ai] owner_address`. Verify with `tests/config/test_config.py` cases for a minimal user, no users, and the old keys being rejected as unknown.
- [x] 1.2 Validate users: duplicate names and Telegram IDs, exactly one admin, `mcp_tokens` keys naming configured servers, allow-list prefixes (`alfred_`, `<server>_`, `*`), and exact `alfred_*` names that exist. Report all problems together. Verify with config tests for each `service-configuration` and `users` scenario.
- [x] 1.3 Resolve each user's MCP servers with their token (falling back to the server's `token_env`), allowing duplicate MCP token values, and never putting a secret in errors. Verify with config tests for user token reference, missing variable and shared token.
- [x] 1.4 Update `alfred.toml.example` with two commented `[[users]]` entries (admin with `tools = ["*"]`, a second user with a partial allow-list and `mcp_tokens`). Verify that the example loads in the existing example-config test.

## 2. Database: users migration

- [x] 2.1 Add migration `0005_users`: `user_name` on conversations, queue_items and attachments and `created_by` on reminders, filled from `ALFRED_MIGRATE_ADMIN` and then made `NOT NULL`; the `reminder_recipients` table, with the admin as recipient of existing reminders; the stored kind `'owner'` renamed to `'user'` in entries and queue_items, with their CHECK constraints; and the per-user current-conversation unique index replacing the global one. Add a working downgrade. Update `db/models.py` (`ENTRY_KINDS`, `QUEUE_KINDS`). Verify on Postgres: upgrade with legacy rows assigns them to the admin and renames kinds; upgrade of an empty database needs no variable; upgrade with rows and no or an invalid variable raises `MigrationError` and changes nothing; downgrade restores 0004.
- [x] 2.2 At `serve` startup, warn about any `user_name` in the database that is not a configured user. Verify with a db test that the warning names the unknown user and no warning is logged when all are configured.

## 3. Turn input, allow-list gate and per-user history

- [x] 3.1 Add `AllowList` and the `Gated` wrapper to `tools/base.py`, add `user` to `TurnInput`, rename the turn kind `owner` to `chat` and `owner_only` to `chat_only`, and wrap every opened toolset in `agent/turn.py`. Verify with unit tests for matching (exact, `prefix*`, `*`, empty), hidden definitions, and an "unknown tool" result for a gated name.
- [x] 3.2 Make `History` per user (current conversation, append, recent, reset) with lazy creation of each user's first conversation. Verify with `tests/test_history.py` cases for separate conversations, a reset that leaves the other user untouched, and notices recorded for the target user.
- [x] 3.3 Add `user_name` to queue items (`enqueue_user`, `enqueue_ask(..., user)`), resolve the `User` in the worker, and fail items naming an unconfigured user with a reason. Verify with `tests/test_queue.py`.
- [x] 3.4 Make `BrainTools` admin-only (chat turn, admin, at least two brains) and send the fallback notice to the turn's user plus the admin when they differ. Verify with `tests/agent/test_brains.py` and a turn test for a fallback on a non-admin turn.

## 4. Telegram, outbox and notices per user

- [x] 4.1 Gate `telegram/bot.py` on the configured users' Telegram IDs (private chats only) and enqueue as that user, including attachments and albums. Verify with `tests/telegram` cases for the owner, a second user, a stranger and a group.
- [x] 4.2 Address `Outbox.send` and the typing indicator to a user's chat. Make every fixed notice in `notices.py` and `queue.py` use the concerned user's address and chat. Verify with `tests/test_notices.py` and queue tests that a failure on `ana`'s turn reaches only her, ending in her address.

## 5. Prompts built for the turn's user

- [x] 5.1 Split `OPERATING_RULES` into always-on paragraphs and paragraphs guarded by offered tool names (web, reminders with `for` and other user names, calculation). Add the "you are talking to <name>" line, use the user's address in the persona, and list other brains only when the brain tool is offered. Replace "the owner" wording. Verify with `tests/agent/test_prompts.py` for each `agent-loop`, `persona`, `model-selection` and `users` prompt scenario.

## 6. Memory and attachments per user

- [x] 6.1 Scope `alfred_search_conversations` and `alfred_recall_conversation` to the turn's user, with another user's conversation refused like an unknown id. Verify with `tests/memory` for search isolation and the recall refusal.
- [x] 6.2 Write `user_name` on attachments at intake and confirm that the reach check is per user. Verify with `tests/attachments/test_tools.py` for another user's attachment number.

## 7. Reminders with creator and recipients

- [x] 7.1 Extend the reminder store: create with creator and recipients, list and cancel filtered by involvement (not involved is reported as unknown), and `claim_due` queuing one item per recipient in one transaction. Verify with `tests/reminders` store tests for shared, cross-user and not-involved cases, and for exactly-once claiming.
- [x] 7.2 Add `for` to `alfred_remind_set` (default the turn's user, validated, duplicates collapsed). Show creator and recipients in results and the list when they're not just the turn's user. Verify with reminders tool tests for each `reminders` scenario.
- [x] 7.3 Label reminder turns and `reminder` entries with "set by <name>" when the creator isn't the recipient, and deliver each recipient's turn and fallback notice independently. Verify with a turn test (`tests/agent/turn_helpers.py`) for a reminder `diogo` set for `ana`, and a shared reminder where one recipient's turn fails.

## 8. MCP and the service API

- [x] 8.1 Make `McpTools` pick each user's servers and tokens, skip servers the allow-list can't match, keep instructions only when a tool survives the gate, and warn about unknown exact allow-list names at listing time. Verify with `tests/mcp` using an in-process server: per-user token header, skipped server, instructions dropped.
- [x] 8.2 Add the optional `user` to `/notify` and `/ask` (default admin, 422 for an unknown user) and deliver and record for the target user. Verify with `tests/test_api.py` for named, default and unknown user.

## 9. Integration and release

- [x] 9.1 Add a whole-turn test with two configured users: `ana` (partial allow-list) and `diogo` (`*`). Check offered tools, isolated history and memory, and a cross-user reminder end to end. Verify that `make lint` and `make test` pass.
- [x] 9.2 Update `AGENTS.md` ("Adding a tool group": the allow-list gate and the `chat_only` rename). Bump the version in `pyproject.toml`. Verify that `make image` tags the new version.
- [x] 9.3 Write the handover update for this release (new `[[users]]` block, removed keys, new MCP token env vars, `ALFRED_MIGRATE_ADMIN` for the migrate step, migrate then serve, rollback note). Verify that the file exists under `handover/updates/`.

## Workflow follow-up

- Archive the change once implemented and deployed, and sync its specs.
