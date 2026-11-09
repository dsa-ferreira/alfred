# Proposal

## Why

The owner wants alfred to remind them of things at a given time ("remind me
to call my mother at five"). Today the only option is the Trello MCP server,
which keeps to-dos but never speaks up at a time. A time-bound reminder has
to come from alfred itself, at the right moment, in the Telegram chat.

## What Changes

- New local tools in owner turns: `alfred_remind_set`, `alfred_reminders_list`
  and `alfred_reminder_cancel`. Set takes the reminder text and either
  `in_minutes` or `at` (a local `HH:MM`, `YYYY-MM-DD HH:MM` or `YYYY-MM-DD`).
  alfred resolves the time in the owner's timezone and returns the day and
  time as a person says them ("today at 17:00"), which the model confirms in
  its own words, without a reminder number. The model never does date arithmetic and
  never needs the time of day; the system prompt keeps only the date.
- Reminders are stored in Postgres and survive restarts. A new background
  loop checks for due reminders about every 30 seconds (configurable). It
  makes no AI requests while nothing is due.
- A due reminder becomes a new kind of queue item, `reminder`. Its turn is
  written by the model, like an `/ask` turn, but it always reaches the owner:
  no `NO_REPLY`, and if the turn fails for good, a fixed notice with the
  reminder text is sent instead. It is recorded in the conversation, so the
  owner can answer it ("snooze 15 minutes", "done").
- Reminders missed while alfred was down are sent once it is back, however
  late. The model is told when the reminder was due and when it is sent.
- Operating rules say where reminders go: a request that says "task" or
  "todo" goes to the owner's to-do list (the Trello MCP server), even when it
  has a time; any other request with a time or date is an alfred reminder;
  anything else goes to the to-do list.
- New `[reminders]` config section: `default_time` (default `09:00`, used for
  a date with no clock time) and `check_interval_s` (default 30).
- Out of scope: recurring reminders, an edit tool (move = cancel + set),
  reminders set by services through `/ask`.

## Capabilities

### New Capabilities
- `reminders`: setting, listing and cancelling timed reminders, resolving
  their time, firing them when due (including after downtime), and how the
  owner is told.

### Modified Capabilities
- `message-queue`: a third kind of queue item, `reminder`, that shares the
  single ordered worker and the unreachable-AI retry.
- `conversation-history`: delivered reminders are recorded in the current
  conversation and shown to the model labelled as reminders.
- `agent-loop`: operating rules cover reminder turns and the reminder versus
  to-do routing; reminder turns get their own input and no `NO_REPLY`.
- `persona`: a new fixed notice that delivers a reminder when its turn fails.
- `service-configuration`: the `[reminders]` settings.

## Impact

- Code: new `src/alfred/reminders/` package (store, time resolution, tools,
  loop); `agent/turn.py` (tools, reminder turn input), `agent/prompts.py`,
  `queue.py` (reminder items), `history.py` (new entry kinds), `db/models.py`,
  `notices.py`, `config.py`, `app.py` (fifth background task).
- Database: migration `0003` adds a `reminders` table and extends the
  `entries.kind` and `queue_items.kind` check constraints.
- Config: new optional `[reminders]` section; `alfred.toml.example` and the
  README updated. No new secrets, services or dependencies.
- Deployment: the next release needs a handover update for the migration
  and the new config section.
