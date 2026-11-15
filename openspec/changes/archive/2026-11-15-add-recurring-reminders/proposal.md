# Proposal

## Why

Reminders are one-shot, and a reminder turn is told not to use tools. So
alfred can say "call mum" at five, but not do something on a schedule, like
"every weekday at 8, brief me on the weather, what's downloading and my
reminders for today". Most of the machinery is already there: a due reminder
runs as an AI turn in the recipient's conversation, with the web and MCP
tools offered. What's missing is a repeat rule, permission to act, a way to
stay quiet when there's nothing to say, and conversation handling that
copes with a message every day. OpenJarvis's scheduler (cron, interval,
once, pause/resume) was the reference; this change keeps alfred's single
reminder concept and leaves out cron, intervals and pause.

## What Changes

- **Repeating reminders.** `alfred_remind_set` takes an optional `repeat`
  (`daily`, `weekdays`, `weekends` or `weekly` with `days`) together with an
  `at` of `HH:MM`. alfred works out each occurrence from the rule, in the
  configured timezone, so 08:00 stays 08:00 across DST changes. One reminder
  number covers the whole series. No cron strings, intervals or monthly rules.
- **Reminder turns may act.** The reminder-turn instruction no longer forbids
  tools. A reminder that asks alfred to find out or check something ("brief
  me on the weather") is carried out with the recipient's tools before the
  message is written. One-shot reminders get this too ("at 18:00, tell me if
  Dune finished downloading"). `alfred_reminders_list` is offered in reminder
  turns too, so a briefing can include today's reminders. Setting and
  cancelling still are not.
- **Quiet reminders.** `alfred_remind_set` takes `quiet_ok`, for "only if
  there's something to tell". Only then may the reminder turn answer
  `NO_REPLY`: nothing is sent or recorded. A failed turn still sends the
  fixed reminder notice.
- **Routing.** The routing rules and the tool description say that a request
  to do something later or on a schedule is a reminder, even without the
  word "remind", and is not done now.
- **Listing** shows a series with its rule and next time, and whether it may
  stay quiet.
- **Cancelling.** **BREAKING (behaviour):** a recipient who did not set a
  reminder now cancels it only for themselves. It stays set for the other
  recipients, and ends when its last recipient leaves. The creator still
  cancels for everyone. This applies to one-shot reminders too. Cancelling a
  repeating reminder stops the whole series. There is no pause or resume:
  cancel and set again, as for moving a reminder.
- **After an outage**, a repeating reminder is delivered at most once, for its
  most recent missed occurrence, and then moves on to its next one.
- **A reminder entry keeps its own due time.** The history shows the due time
  of the occurrence that was delivered, not the series' next one, so message
  times never change.
- **Conversations with no message from their user** (only notices, `/ask`
  turns and reminders) are archived when a new notice, `/ask` or reminder
  arrives after the idle close time has passed since their last entry. The
  new entry starts a new conversation. No timer and no marker. A daily
  briefing nobody answers no longer piles up in one conversation for weeks,
  and the latest one stays in context for a late reply.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `reminders`: repeat rules, `quiet_ok`, listing a series, cancelling per
  recipient, one delivery after an outage, reminder turns that may use tools
  and may stay quiet, `alfred_reminders_list` in reminder turns.
- `agent-loop`: "Turn context" (the reminder turn's instruction allows tools
  and, for quiet reminders, `NO_REPLY`), and "Reminder routing rules" (requests
  to do something later or on a schedule, and repeat rules).
- `conversation-history`: "Reminders in the conversation" (the entry's own
  due time), "Idle conversations close" (conversations with no user message),
  and a new requirement for archiving them when a new entry arrives.

## Impact

- **Code:** `reminders/when.py` (repeat rules and next occurrence),
  `reminders/store.py` (series columns, re-arming in `claim_due`, per-recipient
  cancel), `reminders/tools.py` (new arguments, list tool offered on reminder
  turns), `agent/prompts.py` (reminder-turn and routing rules), `agent/turn.py`
  (reminder input), `queue.py` (due time from the queue item, quiet answers),
  `history.py` (entry due time, closing user-less conversations on append),
  `app.py` (toolset wiring), `tools/catalog.py` unchanged (no new tool names).
- **Database:** migration `0008`: repeat columns and `quiet_ok` on `reminders`,
  `reminder_due_at` on `queue_items` and `entries` (backfilled from
  `reminders.due_at`), and a new `end_reason` value.
- **Model server:** one more turn per repeating reminder occurrence, with tool
  calls; one summary request per archived briefing-only conversation (the
  sweeper already summarises every archived conversation).
- **Config:** no new settings.
- **Handover:** an update for the release (migration `0008`).
- **Out of scope:** monthly and interval rules, cron, pause/resume, skipping
  one occurrence, dropping stale occurrences after an outage, memory and fact
  tools in reminder turns.
