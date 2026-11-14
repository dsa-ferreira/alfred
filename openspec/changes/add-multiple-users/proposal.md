# Proposal

## Why

alfred serves exactly one person: one Telegram user ID, one current
conversation, one memory, one set of reminders, and every notice goes to "the
owner". The owner now wants a second person (their partner) to use alfred
too. She needs her own private conversation, and access to only some of the
tools. Service notifications, such as a finished download she asked for,
should reach whoever they are about.

## What Changes

- **BREAKING** Users are configured as `[[users]]` entries (name, Telegram
  user ID, form of address, admin flag, tool allow-list, optional per-server
  MCP token variables). `[telegram] owner_user_id` and `[ai] owner_address`
  are removed. Exactly one user is the admin.
- Messages from any configured user, in their private chat, are accepted.
  Everyone else is still ignored silently.
- Each user has their own current conversation, history window, conversation
  memory (search and recall see only their own archived conversations),
  attachments, and "end conversation" reset.
- Each user has a per-tool allow-list. A tool is offered on a user's turn only
  if the turn's existing rules offer it and the allow-list matches its
  model-facing name (exact names, or `*` patterns such as `alfred_*` or
  `cookbook_*`). This replaces "owner turns only" as the per-person gate.
- The system prompt is built for the user whose turn it is. It uses their
  form of address and describes only the tools they are offered.
- Brain switching stays alfred-wide and is an admin feature: the brain tool
  is offered only on the admin's turns. All users' turns use the one current
  brain.
- Reminders get a creator and one or more recipients. A user can remind
  themselves, another user, or several users ("remind us"). Each recipient
  receives the reminder in their own conversation. Any recipient or the
  creator can list and cancel it.
- `/notify` and `/ask` accept an optional `user`. Without it they go to the
  admin. The notice, or the ask turn, goes to that user's chat and
  conversation, and an ask turn uses that user's tools.
- MCP servers can be called with a per-user token. A user's entry names a
  token variable per server, and without one the server's own `token_env` is
  used. This lets a server tell users apart later (for example, so mcparr
  knows who requested a download).
- Fixed notices (failures, fallback, reminders, unsupported messages) go to
  the user they concern, with that user's form of address.
- Existing conversations, reminders and attachments are assigned to the admin
  on upgrade.

## Capabilities

### New Capabilities
- `users`: who may talk to alfred, the admin role, and each user's tool
  allow-list and form of address.

### Modified Capabilities
- `telegram-chat`: accept every configured user instead of one owner. Replies,
  notices and typing go to the user's own chat.
- `conversation-history`: one current conversation per user. Notices, ask
  turns and reminders are recorded in the target user's conversation.
- `conversation-memory`: search and recall only cover the turn user's own
  archived conversations. Offering is gated by the allow-list.
- `reminders`: creator and recipients, cross-user reminders, per-recipient
  delivery, and listing and cancelling by any recipient or the creator.
- `service-api`: optional `user` on `/notify` and `/ask`, defaulting to the
  admin. An unknown user is rejected.
- `service-configuration`: `[[users]]` replaces `owner_user_id` and
  `owner_address`, with new validation rules and per-user MCP token
  variables.
- `model-selection`: the brain tool is admin-only, and other users see only
  the current brain in their prompt.
- `mcp-tools`: per-user tokens, and MCP tools filtered by the user's
  allow-list.
- `agent-loop`: the system prompt is for the turn's user, and the operating
  rules are adjusted to the tools offered.
- `persona`: the form of address and fixed notices are per user. The persona
  text stays alfred-wide.
- `attachments`: attachments belong to the user who sent them, and the tools
  reach only that user's attachments.

## Impact

- **Config:** a new `[[users]]` section and removed keys (a breaking change
  for the deployed `alfred.toml`). `alfred.toml.example` and the handover
  update must say so.
- **Database:** a new migration adds a required user column to
  conversations, queue items, reminders and attachments, and a
  reminder-recipients table. It renames the stored message kind `owner` to
  `user`, and makes the unique "one current conversation" index per user. It
  assigns existing rows to the admin named by `ALFRED_MIGRATE_ADMIN`, which
  is needed only when upgrading a database that has data.
- **Code:** `config.py`, `telegram/bot.py` (gate, outbox per chat), `queue.py`,
  `history.py`, `memory/*`, `reminders/*`, `attachments/*`, `api.py`,
  `agent/turn.py`, `agent/prompts.py`, `notices.py`, `tools/base.py`
  (`TurnInput` carries the user, plus an allow-list filter), `mcp/*` (token
  per user), and `app.py`.
- **External:** the homelab sets one new env var per user per MCP server (it
  may hold the same value as today's token). Services may start sending
  `user`. No new dependencies.
