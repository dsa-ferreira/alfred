# Tasks

## 1. Schema

- [x] 1.1 Add migration `0008_recurring_reminders` and the model columns (design D1): `repeat`, `repeat_days`, `repeat_time`, `quiet_ok` on `reminders` with their CHECKs, `reminder_due_at` on `queue_items` and `entries` backfilled from `reminders.due_at`, and `unattended` in `END_REASONS`, with the downgrade from the migration plan. Verify in `tests/db/test_schema.py` on Postgres: an existing reminder entry and pending reminder item come out with `reminder_due_at` equal to the reminder's `due_at`; the CHECKs reject an unknown `repeat`, a `repeat` without `repeat_time`, and `repeat_days` outside 1–127; `end_reason = 'unattended'` is accepted; downgrade to 0007 cancels a pending repeating reminder and turns an `unattended` conversation into `idle` with `marker_sent_at` set.

## 2. Repeat rules

- [x] 2.1 Add to `reminders/when.py` (design D2): parsing of `repeat`, `days` and an `HH:MM` `at` into a rule (mask and clock), with `WhenError`s naming the accepted forms; `next_occurrence(mask, clock, after, zone)`; and `format_rule`. Verify in `tests/reminders/test_when.py`: weekdays set on Sunday 15 Nov 2026 14:47 gives Monday 16 Nov 08:00; weekly tue/fri at 21:00 on Tuesday gives Friday 20:00; daily 08:00 at 07:30 gives today; `Europe/Lisbon` daily 08:00 after Saturday 24 Oct 2026 08:00 gives Sunday 25 Oct 08:00 local; 01:30 on the spring-forward day gives 02:30; every error case in the "Repeat rule errors" requirement; `days` accept `Tue`, `tuesday` and `tue`; `format_rule` gives "every day", "every weekday", "every weekend day" and "every Tuesday and Friday" at the time.

## 3. Store

- [x] 3.1 Extend `Reminders.set` and `ReminderRow` with the rule and `quiet_ok`, and give `Reminders` the zone. Make `claim_due` re-arm repeating reminders (design D3): queue items carry `reminder_due_at`, the latest missed occurrence is delivered once, `due_at` moves to the next occurrence after now, and the status stays `pending`. One-shots also set `reminder_due_at`. Verify in `tests/reminders/test_store.py` on Postgres: a weekdays reminder claimed on Friday 08:00 queues one item per recipient for Friday 08:00 and is next due Monday 08:00; claimed on Wednesday 10:00 after being due Monday 08:00, it queues one item for Wednesday 08:00 and is next due Thursday 08:00; it still appears in `pending`; a one-shot behaves as before with `reminder_due_at` set.
- [x] 3.2 Make `Reminders.cancel` cancel per recipient (design D5): the creator cancels for all, a non-creator recipient is removed (outcome `left` with the remaining recipients), and the last recipient leaving cancels it. Verify in `tests/reminders/test_store.py`: `ana` cancelling #15 (diogo's, for both) leaves it pending for `diogo` only and out of `ana`'s `pending`; `diogo` cancelling it cancels it for both; `ana` cancelling #14 (diogo's, for her alone) cancels it; a queue item already claimed for `ana` is untouched; cancelling a repeating reminder stops it from being claimed again.

## 4. Tools and prompts

- [x] 4.1 Add `repeat`, `days` and `quiet_ok` to `alfred_remind_set`, with the set result for a series, the list line with rule and "may stay quiet", and the `left` cancel result (design D4, D5). Update the tool descriptions. Verify in `tests/reminders/test_tools.py`: the "Result" scenario's text with "every weekday at 08:00" and "tomorrow at 08:00" and no number; errors store nothing; the list shows #31 once with its next time and rule; `ana`'s cancel of #15 says it stays set for `diogo`.
- [x] 4.2 Offer only `alfred_reminders_list` on reminder turns through `ReminderTools.open` (design D4). Verify in `tests/reminders/test_tools.py` that a chat turn gets all three, a reminder turn gets only the list (and the recipient's reminders), an `/ask` turn gets none, and a reminder turn for a user whose allow-list lacks `alfred_reminders_list` gets none.
- [x] 4.3 Rewrite `REMINDER_RULE` with the quiet and not-quiet endings and the `reminder_quiet` argument, and extend `REMINDER_ROUTING_RULE` for scheduled requests, `repeat` and `quiet_ok` (design D6). Verify in `tests/agent/test_prompts.py`: the reminder rule no longer says not to use tools and tells the model to look up what the reminder asks for; only the quiet variant mentions `NO_REPLY`; the routing rules contain the "every weekday at 8, brief me on the weather" example and the "only if" example; a turn without `alfred_remind_set` has no routing rules.

## 5. Reminder turns and delivery

- [x] 5.1 Add `repeat_label` and `quiet_ok` to `TurnInput`. In `Worker._reminder_turn`, take the due time from `item.reminder_due_at` and the rule and `quiet_ok` from the row. Put the rule in the reminder input (design D6). Verify in `tests/agent` with `turn_helpers.py`: #31's input on Tuesday shows due Tuesday 08:00 and "every weekday at 08:00" even though the row is already due Wednesday; #12's input is unchanged in form.
- [x] 5.2 In `Worker._complete`, finish a quiet reminder's `NO_REPLY` with nothing sent or recorded, and keep the fixed notice for blank answers, non-quiet `NO_REPLY` and failed turns (design D6). Verify in `tests/test_queue.py`: quiet + `NO_REPLY` sends nothing and adds no entries; quiet + HTTP 400 sends the fixed notice; non-quiet `NO_REPLY` sends the fixed notice; quiet with an answer sends and records it.
- [x] 5.3 Whole-turn test of a briefing in `tests/reminders/test_in_turn.py`: a scripted model calls a fake weather tool and `alfred_reminders_list` on #31's turn for `diogo`, and his reply is sent and recorded. A second case: a chat turn "every weekday at 8, brief me on the weather" where the scripted model calls `alfred_remind_set` with `repeat` `weekdays` gets the series stored. Update the README reminders section (repeating, quiet, scheduled briefings, who can cancel).

## 6. History

- [x] 6.1 Store `reminder_due_at` on entries and read it in `History.recent` instead of the joined `Reminder.due_at` (design D7). Verify in `tests/test_history.py`: two deliveries of #31 (Monday and Tuesday) are labelled with their own due times after the row moved on; old entries from before the migration keep their label.
- [x] 6.2 Close user-less conversations on append (design D7): `History` takes the idle close time and an `on_archive` callback; for `notice`, `ask_prompt` and `reminder` entries it archives a user-less current conversation whose newest entry is older than the idle time as `unattended`, starts a new one and records the entry there; wire it in `app.py` with the sweeper nudge. Update the README `history.idle_close_after_h` row. Verify in `tests/test_history.py` and `tests/test_idle.py`: every scenario of "Conversations without user messages close when something arrives" (including the reply entry staying with its reminder, the earlier idle marker naming the new current conversation, and idle close off); a retried reminder item doesn't close the conversation it was already recorded in; a conversation with a user message is never closed this way; `on_archive` is called; the archived conversation is summarised by the sweeper and its fact extraction is done without a model request.

## 7. Integration

- [x] 7.1 Run `make lint` and `make test` and verify both pass.
- [x] 7.2 End-to-end against `ai.lan` (`make run`): in Telegram, ask "every weekday at 8, brief me on the weather, what's downloading and my reminders for today" and check that the reply confirms the series without fetching anything now and that `alfred_reminders_list` shows it. Set a `daily` one a couple of minutes ahead and check the briefing uses the tools. Set "in 2 minutes, tell me only if something is downloading" with nothing downloading and check nothing arrives. If the model routes the request wrong, stop and show the owner the turn log before tuning the prompt.

## Workflow follow-up

- Bump the version before building images, then add a handover update (migration `0008`, no new config).
- Archive the change after review.
