# Design

## Context

See proposal.md for the motivation. Today "the owner" is implicit everywhere:

- `config.telegram.owner_user_id` gates `telegram/bot.py`, and the same ID is
  the chat that `TelegramOutbox` sends to.
- `conversations` has a unique partial index on `(ended_at IS NULL)`, so
  there is one current conversation globally. `History`, the memory search,
  the reminders store and the attachment lookups all assume it.
- `TurnInput.kind` is `owner | ask | reminder`, and `owner_only(turn)` is the
  only per-turn tool gate (memory, reminders, attachments, end conversation,
  brains).
- `agent/prompts.py` has one static `OPERATING_RULES` text that mentions
  every alfred tool, and `owner_address` comes from `[ai]`.
- `McpTools` gets one `McpServer` (with one token) per server from the
  config. The connector already takes the `McpServer` it connects with.
- `alfred migrate` deliberately needs only `ALFRED_DATABASE_URL`, not the
  config.

## Goals / Non-Goals

**Goals:**
- Make the user a first-class part of every turn, queue item and stored row,
  with isolation enforced in the data access code rather than by the model.
- Gate tools per user in one generic place, so a new toolset gets the gate
  for free.
- Keep the single serial queue and the single alfred-wide brain.

**Non-Goals:**
- Per-user timezone, persona text, brains, history window or other tunables.
  These stay alfred-wide.
- Users added or changed at runtime. Users come from config, and a change
  needs a restart.
- Admin access to other users' conversations or memory.
- Limiting which services may target which users.
- Changes to mcparr or a download notifier so they send `user`. Those happen
  outside alfred.

## Decisions

### D1. Users live in config and rows store the user's name

A `User` value (name, telegram_user_id, address, admin, allow-list, resolved
MCP servers) is built by `load_config`. Rows that belong to a user get a
`user_name` text column: `conversations`, `queue_items`, `reminders`
(`created_by`) and `attachments`. There is no `users` table.

- *Why:* config is already the source of truth for users. A table would have
  to be synced from config at startup and would add a join everywhere for no
  gain. The name is also what `/notify` and the model use.
- *Alternative:* a `users` table with integer IDs, which makes renames free.
  Rejected because renames are rare and can be handled by a manual `UPDATE`.
  D9 warns when the database names a user the config doesn't know.

Entries don't get a column. They belong to a conversation, which belongs to
a user. The stored kind `'owner'` is renamed to `'user'` in both
`entries.kind` and `queue_items.kind`, meaning "the conversation's user wrote
this". Migration 0005 rewrites the existing rows and the CHECK constraints,
and `ENTRY_KINDS` and `QUEUE_KINDS` change to match. That avoids leaving a
misleading "owner" label on another user's messages. In code, the turn kind
becomes `chat` (`TurnInput.kind: chat | ask | reminder`), since a turn is
started by a chat message, and `owner_only` becomes `chat_only`.

### D2. `TurnInput` carries the `User`

`TurnInput` gains `user: User`. The worker resolves it from the queue item's
`user_name` (an unknown name fails the item with a logged reason, see D9).
Everything downstream (history, memory, reminders, attachments, prompts,
notices, outbox, MCP) reads `turn.user` instead of global config. The
`AttemptContext.owner_texts` used by the web-fetch allowlist keeps its
meaning: user-kind entries from this user's own window.

### D3. One generic allow-list gate in the turn loop

`alfred.tools.base` gets an `AllowList` matcher (exact names, `prefix*`,
`*`) and a wrapper `Gated(open_toolset, allow)`. It filters `definitions`,
makes `handles(name)` false for names that don't match (so `dispatch` returns
"unknown tool"), and passes everything else through. `turn.py` wraps every
opened toolset with the turn user's allow-list. No toolset implements the
gate itself. The existing `offered(turn)` rules (chat-only, vision, at least
two brains) stay in each toolset, and the allow-list is applied on top of
them. That is also how the "offered on every turn" requirements in
`calculator`, `date-tools` and `web-tools` now read: offered on every turn,
subject to the user's allow-list.

- *Alternative:* each toolset checks the allow-list in `offered`. Rejected:
  that check would be duplicated in ten places and forgotten in the next one.

`McpTools.open(turn)` additionally uses the allow-list *before* connecting:
servers that no entry could match are skipped (no listing, no instructions,
no "unavailable" note), and the listed tools are then filtered by the generic
gate. Instructions are kept only if at least one of the server's tools
survives the gate (`mcp-tools` delta). Unknown exact MCP entries are warned
about once per server listing, like `exclude_tools`.

Brain switching is admin-only by rule, not just by allow-list:
`BrainTools.offered` returns `turn.kind == "chat" and turn.user.admin and
len(brains) > 1`.

### D4. The system prompt is built from the offered tool names

`build_system_prompt` receives the turn's user and the set of tool names left
after the gate. `OPERATING_RULES` is split into paragraphs, each guarded by
the tools it talks about:

```
always:             Telegram chat with <user>, tool use, notices/ask/recall/
                    reminder message formats, message time stamps
web_search|fetch:   web rules
remind_set:         reminder routing + "for" + "other users: ..."
calculate|date:     calculation routing (names only offered ones)
attachments:        already conditional (attachments design D8)
brain tool offered: "other brains: ..." in the brain line
```

The persona is formatted with `turn.user.address`. The text "the owner" in
the prompts becomes "the user", or uses the user's name where the model needs
it. The paragraphs stay together in `prompts.py` so the prompt can be read in
one place. They are not spread into each toolset's `prompt_notes`.

### D5. One current conversation per user

The `uq_conversations_one_current` index becomes unique on `(user_name)`
`WHERE ended_at IS NULL`. `History` methods take the user name:
`current_conversation_id(user)`, `append(user, ...)`, `recent(user, n)`,
`reset(user)`. `current_id` creates the user's first conversation lazily,
the same way it creates the first one today. Memory search and recall add
`conversations.user_name = :user` to every query. Recall of another user's
id goes through the same "no such conversation" path. Attachment reach is
already "current conversation plus recalled ones", so it becomes per user
with no extra rule. The summariser and sweeper work per conversation and
don't change. The reset marker goes to the conversation's user.

### D6. Outbox and typing are addressed per user

`Outbox.send(user, text)`. `TelegramOutbox` sends to `user.telegram_user_id`
(a private chat's ID equals the user's ID). The typing indicator gets the
chat ID per turn. `Bot` builds a `telegram_user_id -> User` map for the gate,
and enqueues `enqueue_user(user, text, message_id, attachments)`.

### D7. Reminders: a creator plus a recipients table

```
reminders(id, text, due_at, status, created_by, ...)
reminder_recipients(reminder_id FK, user_name)  PK(reminder_id, user_name)
queue_items(..., user_name, reminder_id)
```

- `alfred_remind_set` gains `for: [names]`, which defaults to the turn's user
  and is validated against configured users (an error lists them).
  Duplicates are collapsed.
- List and cancel filter on `created_by = :u OR EXISTS recipient = :u`. Not
  being involved is reported exactly like an unknown number.
- `claim_due` stays one transaction. It marks the reminder `fired` and
  inserts one queue item per recipient, ordered by due time and then
  recipient name. Status stays on the reminder rather than per recipient,
  because cancelling cancels it for everyone (an agreed decision) and firing
  happens for all recipients at once.
- Each recipient's reminder turn and its fallback notice run independently,
  so a failure for one doesn't affect the other.
- The reminder input and the `reminder` entry carry `set by <name>` when
  `created_by` differs from the recipient (`history.to_messages` formats it).

- *Alternative:* one reminder row per recipient. Rejected: "cancel #15" would
  no longer cancel the shared reminder, and listing would show duplicates.

### D8. Per-user MCP tokens resolved at startup

`[[users]] mcp_tokens = { server = "ENV_VAR" }`. `load_config` resolves each
user's servers to `McpServer` copies carrying that user's token, falling back
to the server's own `token_env`. `McpTools` receives a
`servers_for(user)` function instead of a fixed list. The connector is
unchanged. Unlike service tokens, duplicate MCP token values are allowed.

### D9. The migration assigns existing data to the admin, named by an env var

`alfred migrate` stays config-free, so migration `0005_users` takes the
admin's name from `ALFRED_MIGRATE_ADMIN`. In one transaction it:
- adds `user_name` to `conversations`, `queue_items` and `attachments`, and
  `created_by` to `reminders`;
- creates `reminder_recipients`;
- sets every existing row's user to `ALFRED_MIGRATE_ADMIN`, and adds that user
  as the recipient of every existing reminder;
- makes those columns `NOT NULL`;
- renames the stored kind `'owner'` to `'user'` in `entries` and
  `queue_items`, and updates their CHECK constraints (D1);
- drops `uq_conversations_one_current` and creates
  `uq_conversations_one_current_per_user` on `(user_name) WHERE ended_at IS
  NULL`.

`ALFRED_MIGRATE_ADMIN` is required only when there are rows to assign. A
fresh database migrates without it. If rows exist and the variable is unset,
empty, or not a valid user name (lowercase letters, digits and hyphens), the
migration raises `MigrationError` with a message saying to set it to the
admin's `[[users]] name`, and changes nothing. The migration can't check the
name against the config. So at `alfred serve` startup, alfred logs a warning
for any `user_name` in the database that is not a configured user. A typo
then shows up right away, and is fixed by a manual `UPDATE`. Queue items for
an unconfigured user are marked failed with that reason when they reach the
head of the queue, instead of blocking it.

- *Why:* the database itself guarantees that every row has a user. No NULL
  state needs explaining, and there is no startup backfill code to keep
  around.
- *Alternative:* nullable columns plus a backfill at `alfred serve` startup,
  which needs no extra variable. Rejected because a stray NULL row would be
  silently invisible to every user, and the guarantee would rest on code
  rather than the schema.

### D10. Service API target user

`NotifyRequest` and `AskRequest` gain `user: str | None`. The handler
resolves it against the configured users. If it is unknown, it responds 422
with `unknown user '<name>'` and does nothing. `None` means the admin.
`enqueue_ask(source, prompt, user)`. `/notify` delivery and recording use the
target user's chat and conversation.

### D11. Fallback notice also reaches the admin

The brain is alfred-wide, so a fallback on another user's turn changes the
admin's choice. The worker sends the fallback notice to the turn's user, and
also to the admin when they are someone else (each with their own address).
The admin's copy is not recorded in the admin's conversation. It is
operational, like today's notices that aren't recorded.

## Risks / Trade-offs

- [The model leaks one user's data to the other] → Isolation is enforced in
  queries (D5, D7) and attachment reach, never by prompt. Tests cover each
  "another user's ..." scenario at the store level.
- [Allow-list typos silently hide tools] → Local names and prefixes are
  checked at startup, and MCP exact names are warned about at listing time.
- [Breaking config change blocks startup after upgrade] → Strict validation
  names `owner_user_id` and `owner_address` as unknown keys. The handover
  update gives the new `[[users]]` block to paste.
- [Renaming a user orphans their rows] → Startup warning (D9). The fix is
  a documented manual `UPDATE`.
- [A shared reminder costs one model turn per recipient] → Acceptable for two
  users. Each recipient gets a reply in their own context.
- [Her turns wait behind long admin turns in the single queue] → Accepted
  (agreed). The typing indicator only shows once her turn starts.
- [`ALFRED_MIGRATE_ADMIN` is forgotten or misspelt on upgrade] → If it is
  missing, the migration refuses with a message naming the variable. If it
  is misspelt, the startup warning names the unconfigured user. The handover
  update lists the variable.

## Migration Plan

1. Bump the version. Build and push the image.
2. In the homelab: replace `[telegram] owner_user_id` and `[ai]
   owner_address` with `[[users]]` entries (admin first). Add each user's
   MCP token env vars (they may hold today's token values).
3. Run `alfred migrate` (0005) with `ALFRED_MIGRATE_ADMIN` set to the
   admin's `[[users]] name`, then start `alfred serve`. Check the log for an
   unconfigured-user warning.
4. Rollback: stop alfred, `alembic downgrade` to 0004. This renames `'user'`
   back to `'owner'`, drops the new columns and table, and restores the
   global index. The index only fits while there is one current
   conversation in total, so if a second user has chatted, end or delete her
   conversation first. Her rows otherwise lose their owner on downgrade.
   Then deploy the previous image with the previous config.

The handover update for this release covers steps 2 to 4.
