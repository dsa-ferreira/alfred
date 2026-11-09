# Tasks

## 1. Configuration

- [x] 1.1 Add `RemindersConfig` (`[reminders]`: `default_time` as validated `HH:MM`, `check_interval_s` positive, defaults `09:00` and 30) to `config.py` per design D9; verify "Reminder defaults" and "Invalid default time" in `tests/config/test_config.py`
- [x] 1.2 Document `[reminders]` in `alfred.toml.example` and the README config table; verify `tests/config/test_example.py` passes

## 2. Schema

- [x] 2.1 Add the `Reminder` model, `reminder_id` on `QueueItem` and `Entry`, and the new kinds to `QUEUE_KINDS` and `ENTRY_KINDS` in `db/models.py`, plus migration `0003` with upgrade and downgrade (design D3); verify in `tests/db/test_schema.py` that the migrated schema matches the models and that downgrade to `0002` works

## 3. Time resolution

- [x] 3.1 Add `reminders/when.py`: parse `in_minutes` / `at` (three forms), resolve in the owner's timezone against an injected now, reject past times, handle DST gaps and repeats, and format dates and "in …" durations (design D2); verify every "Setting a reminder", "Clock time without a date" and "Date with or without a clock time" scenario in `tests/reminders/test_when.py`, plus a spring-forward gap, an autumn repeat and a midnight rollover

## 4. Store and tools

- [x] 4.1 Add `reminders/store.py` (`Reminders`): set, list pending soonest first, cancel pending only (error for unknown, fired or cancelled), and claim due reminders into queue items in one transaction (design D3, D4); verify against Postgres in `tests/reminders/test_store.py`, including claiming in due order and a claimed reminder not being claimed twice
- [x] 4.2 Add `reminders/tools.py` with the three tool definitions (descriptions repeat the reminder vs to-do split), argument parsing and result texts; verify "Both or neither", "Empty text", "Listing reminders" and "Cancelling a reminder" scenarios in `tests/reminders/test_tools.py`

## 5. Due-reminder loop

- [x] 5.1 Add `reminders/loop.py`: claim on start and then every `check_interval_s`, signal the queue when anything was claimed (design D4); verify "Reminder comes due", "Nothing due" (no model calls), "alfred down at the due time" and "Days late" in `tests/reminders/test_loop.py` with a fake clock

## 6. Turns, prompts and notices

- [x] 6.1 Add the reminders paragraph and the `[reminder:#<id> …]` explanation to `OPERATING_RULES`, and `REMINDER_RULE`, in `prompts.py` (design D8, D5); verify the "Reminder routing rules" and "Reminder messages explained" scenarios, with and without a custom persona, in `tests/agent/test_prompts.py`
- [x] 6.2 Add `notices.reminder(address, text, due)` (design D6); verify "Reminder notice" with the default and a custom address, and the today versus other-day due format, in `tests/test_notices.py`
- [x] 6.3 In `agent/turn.py`, add the `ReminderTools` protocol, offer and route the three tools in owner turns only, and support `TurnInput` kind `reminder` (per-attempt input with due and now, `REMINDER_RULE`, `/ask`-turn tool set) per design D5; verify "Owner turn", "Ask turn", "Reminder turn", "On time" and "Late" (retry shows the new now) in `tests/agent/test_turn.py`

## 7. Queue and history

- [x] 7.1 Extend `History` with the `reminder` and `reminder_reply` kinds, the `reminders` join for `due_at` in `recent()`, and the `[reminder:#<id> due …]` rendering in `to_messages()` (taking the timezone) per design D7; verify "Reply to a reminder" and "Reminder set in an archived conversation" in `tests/test_history.py`
- [x] 7.2 Render reminder entries in `memory/transcript.py` (design D7); verify in `tests/memory/` that a transcript with a reminder shows `[reminder:#12] call mum` and the reply as `alfred:`
- [x] 7.3 In `queue.py`, process `reminder` items: load the reminder, no typing indicator, record both entries and send the reply on success, use the fixed notice for a blank or `NO_REPLY` reply and for non-retryable failures, keep backoff for retryable ones (design D6); verify "Model error", "Model says NO_REPLY", "AI asleep", "Reminder during an owner turn" and "Snooze" in `tests/test_queue.py`

## 8. Wiring and integration

- [x] 8.1 Build `Reminders` in `app.serve()`, pass the tools to `Agent`, start the `reminders` task (cancelled on shutdown like `memory`), and add it to the startup log line; verify in `tests/test_serve.py`
- [x] 8.2 Describe reminders (routing, time forms, late delivery, the fixed fallback) in the README
- [x] 8.3 Run `make lint` and `make test` and verify both pass
- [ ] 8.4 Against the real model, from a Telegram chat with a local alfred: set "remind me in 2 minutes to stretch", "remind me to buy groceries" and "add a task to call mum at five"; verify the first fires as a model-written reminder, the other two go to Trello, and "snooze 5 minutes" sets a new reminder

## Workflow follow-up

- Bump the version and write a handover update (migration `0003`, the optional `[reminders]` section) with the `homelab-handover` skill.
- Archive the change after review.
