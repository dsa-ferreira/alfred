# Design

## Context

See proposal.md for motivation and specs/reminders for the behavior.

- Local `alfred_*` tools are defined in `agent/turn.py`, added in `run()`
  (owner-only ones under `turn.kind == "owner"`) and routed by name in
  `_run_tool()`. Larger groups live in their own package behind a small
  protocol (`MemoryTools`, `WebTools`).
- The queue (`queue.py`) is one head-of-line worker over `queue_items`,
  with kinds `owner` and `ask`. `_complete()` and `_turn_failed()` decide
  per kind what is recorded and sent. `Queue.signal` wakes the worker.
- History entries have kinds `owner`, `assistant`, `notice`, `ask_prompt`,
  `ask_reply` and `recall`; a turn's entries are unique per
  `(queue_item_id, kind)`, so a retried turn records them once.
  `to_messages()` labels non-owner entries (`[notice:…]`, `[ask:…]`,
  `[recall:…]`); `memory/transcript.py` renders them again for summaries.
- The system prompt has today's date but no time of day (agent-loop spec).
  The owner rejected adding a time to the prompt, timestamps on messages and
  a clock tool for this purpose.
- Background work runs as named tasks in `app.serve()` (`api`, `telegram`,
  `worker`, `memory`); any one stopping shuts alfred down.

## Goals / Non-Goals

**Goals:**
- Correct due times without the model ever doing time arithmetic.
- A reminder is queued exactly once and is never lost: not by a crash, not
  by downtime, not by a failed or silent model turn.
- No AI traffic while no reminder is due.

**Non-Goals:**
- Recurring reminders; editing (cancel + set covers it).
- Sub-minute due times. Due times are whole minutes; a reminder is queued at
  second `:00` of its minute and reaches the owner after the queue and model
  latency.
- Keyword matching in code. Routing between reminders and the to-do list is
  the model's job, from the operating rules.

## Decisions

### D1. A `reminders` package with local tools

```
src/alfred/reminders/
  when.py    parse in_minutes / at, resolve in the owner's timezone, format "today at 17:00", "in 2 h 13 min"
  store.py   Reminders: set, list pending, cancel, claim due (DB access)
  tools.py   tool definitions, argument parsing, ToolOutcome texts
  loop.py    the due-reminder loop
```

`Agent` takes an optional `ReminderTools` (a protocol in `turn.py`, like
`MemoryTools`) and offers its three definitions in owner turns only.
Tool names: `alfred_remind_set`, `alfred_reminders_list`,
`alfred_reminder_cancel`.

*Alternative:* a reminders MCP server. Rejected: firing needs alfred's
queue and history anyway, and a separate service adds deployment for three
small tools.

### D2. alfred resolves the time

`alfred_remind_set(text, in_minutes?, at?)`. `when.py` resolves against
`now` (injected, for tests) in `[ai] timezone`:

| input              | resolves to                                      |
| ------------------ | ------------------------------------------------ |
| `in_minutes: n`    | now + n minutes, seconds dropped (n >= 1, at most one year) |
| `at: "HH:MM"`      | next occurrence, strictly after now              |
| `at: "YYYY-MM-DD HH:MM"` | that local time; error if not after now    |
| `at: "YYYY-MM-DD"` | that date at `[reminders] default_time`; same check |

Every due time is a whole minute: `in_minutes` rounds down to the minute
shown to the owner (13:55:37 + 2 min is 13:57:00), so the reminder fires at
the time they were told.

Anything else is an error naming the three `at` forms. Text is stripped,
must be non-empty, and is capped at 500 characters (error above that).

Local times are built with `zoneinfo`, `fold=0`. A time in a DST gap (it
does not exist) is normalised by a round trip through UTC, which moves it
forward by the gap; an ambiguous time takes the first occurrence. `due_at`
is stored in UTC.

The result reads, e.g.:
`Reminder set for today at 17:00: call mum. Confirm it to the owner in your
own words with this day and time; don't mention a reminder number.`
The day is said as a person would: "today at", "tomorrow at", "on Friday
at" within the week, else "on Friday 20 November at" (plus the year when it
isn't this one). The model echoes the result, so it carries no number and no
countdown: the owner found "Reminder #1 set: stretch, in 2 minutes (13:57)."
machine-like and wants a butler's confirmation ("Very good. I shall remind
you to stretch at 13:57."). A wrong am/pm guess still shows as the day
("tomorrow at 05:00"). Numbers appear only in the list, where the owner may
need them to cancel; to cancel a reminder just set, the model lists first.

The list gives each reminder's number, time and how far away it is.
Durations show days, hours and minutes, dropping zero parts, rounded up to
the minute, so they match the clock times the owner sees (14:47:20 to 17:00
is "in 2 h 13 min", as 14:47 to 17:00).

*Alternatives:* the model computes an ISO timestamp (needs the time of day
in context, and local models get midnight and DST wrong); a clock tool
(extra request, and the model may skip it). Both rejected in discussion.

### D3. Data model

Migration `0003`:

```
reminders
  id           bigint pk
  text         text not null
  due_at       timestamptz not null
  status       varchar(16) not null default 'pending'   -- pending | fired | cancelled
  created_at   timestamptz not null default now()
  fired_at     timestamptz
  cancelled_at timestamptz
  index ix_reminders_pending_due (due_at) where status = 'pending'

queue_items.reminder_id  bigint null fk reminders.id
entries.reminder_id      bigint null fk reminders.id
QUEUE_KINDS  += 'reminder'
ENTRY_KINDS  += 'reminder', 'reminder_reply'
```

`fired` means "handed to the queue"; the tools call it delivered, since
that is what the owner sees. The reminder's text and due time are read
through `reminder_id`, never copied, so the queue item text is the reminder
text only for logs.

### D4. The due-reminder loop

A fifth task, `reminders`, in `app.serve()`:

```
loop forever:
    claimed = 0
    in one transaction:
        rows = SELECT ... WHERE status='pending' AND due_at <= now()
               ORDER BY due_at, id FOR UPDATE SKIP LOCKED
        for each: UPDATE status='fired', fired_at=now()
                  INSERT queue_items(kind='reminder', reminder_id, text)
    if claimed: queue.signal.set()
    sleep until the next check on the clock: the next multiple of
          [reminders] check_interval_s within the minute, or the next minute
```

Checks are aligned to the wall clock (`:00` and `:30` for 30 seconds) and
always include second `:00` of every minute, so a reminder, whose due time
is a whole minute (D2), is queued at the start of its minute. The sleep is
recomputed from the clock after each pass, so it never drifts.
(Changed after the first deploy, where checks every 30 s from startup and
`in_minutes` keeping its seconds made reminders up to a minute late.)

The first pass runs at startup, which is what sends missed reminders
(however late). Claiming and queueing commit together, so a crash either
leaves the reminder pending (claimed again on restart) or queued (the queue
survives restarts). The query hits the partial index and never touches the
AI.

*Alternative:* sleep until the next `due_at` and wake on set/cancel.
Rejected: more moving parts for a few seconds of precision that the AI
wake-up dwarfs anyway.

### D5. Reminder turns

`TurnInput.kind` gains `"reminder"`, with the reminder's id, text and due
time (the worker loads them through `reminder_id`). In `_attempt()`:

- The input message is
  `[reminder:#12 due Monday 9 November 2026, 17:00; now 17:12] call mum`,
  built per attempt, so a retried turn shows the real lateness.
- The system prompt gets `REMINDER_RULE` in place of `ASK_RULE`: the owner
  asked to be reminded of this; write the short reminder message now, say
  how late it is if it is late, never answer `NO_REPLY`.
- Tools: as in `/ask` turns (web and MCP tools, no owner-only local tools).
- Brain fallback, retries and limits are unchanged.

### D6. Worker: always reach the owner

In `queue.py`:

- **Success:** if the reply is blank or `NO_REPLY`, use the fixed
  reminder notice instead. Record `reminder` (with `reminder_id`) and
  `reminder_reply` entries, send the reply as is (no `[source]` prefix),
  finish `done`.
- **Retryable failure:** unchanged (backoff, no limit).
- **Non-retryable failure:** send and record the fixed reminder notice
  instead of the generic failure notice, then finish `failed`.
- No typing indicator (the owner did not just write).

`notices.reminder(address, text, due)`:
`Your reminder, due at 17:00: "call mum". My apologies for the plain delivery, sir.`
Due time shows `HH:MM`, plus the weekday and date when it is not today.

### D7. History and transcripts

`to_messages()` renders a `reminder` entry as
`[reminder:#12 due Monday 9 November 2026, 17:00] call mum` (`user` role)
and `reminder_reply` as `assistant`. `History.recent()` gets the reminder's
`due_at` by joining `reminders` on `entries.reminder_id`, as it already
joins `conversations` for recalls. Summary transcripts render
`[reminder:#12] call mum` and `alfred: …`. Entries go to whichever
conversation is current at delivery.

### D8. Routing lives in the operating rules

A reminders paragraph in `OPERATING_RULES`, sent in every turn so a custom
persona can't drop it:

```
Reminders: if the owner says "task" or "todo", add it to their to-do list
with the to-do tools (such as Trello), even when it has a time; use the
time as a due date if those tools take one. Otherwise, a request with a
time or date ("at five", "in 20 minutes", "on Friday") is a reminder: call
alfred_remind_set with the time as the owner said it, as in_minutes or at
(HH:MM, YYYY-MM-DD HH:MM or YYYY-MM-DD); never work out times yourself, and
confirm the reminder in your own words with the time the tool returns.
Never mention reminder numbers to the owner, except when listing
reminders. Any other "remind me" goes to the to-do
list. A message starting with [reminder:#<id> ...] is a reminder the owner
set earlier, which alfred delivered; the assistant message after it is
what the owner was told.
```

The rule names no server: the owner's Trello server is in their own
`alfred.toml`, and its tools are prefixed `trello_`, which the model
matches to "such as Trello". If no to-do tools are available, the model
says so (the unavailable-servers note already covers a down server).

### D9. Configuration

```toml
[reminders]
# Time used for a reminder on a date with no clock time ("on Friday"), HH:MM.
default_time = "09:00"
# Checks for due reminders run on the clock: every whole minute and every
# check_interval_s within it (at most 60). Reminders are queued at their minute.
check_interval_s = 30
```

`default_time` is validated as `HH:MM` (00:00 to 23:59). `check_interval_s`
must be above 0 and at most 60: a longer interval couldn't check on every
minute.

## Risks / Trade-offs

- [Model reads "five" as 05:00] → the result and confirmation say
  "tomorrow at 05:00", which the owner sees at once; cancel and set again.
  (A countdown would show it too, but the owner chose plainer confirmations.)
- [Small model ignores the routing rule] → the rule is short and has
  examples; tool descriptions repeat the split (set: "for requests with a
  time; 'task'/'todo' go to the to-do list").
- [A reminder waits behind a long turn or an AI outage] → accepted by the
  owner when choosing model-written delivery; the reminder says how late.
- [A reminder turn calls write tools] → same exposure as `/ask` turns;
  `REMINDER_RULE` says to only write the message.
- [Three more tool definitions in every owner turn] → small; visible in the
  context-size log line.

## Migration Plan

`0003` is additive (new table, two nullable columns, widened check
constraints) and runs at startup like the others. Rollback: downgrade drops
the columns and table and restores the constraints. It fails if reminder
queue items or entries exist, which is acceptable for a home deployment
(delete them first). The release gets a handover update for the new
`[reminders]` section and the migration.
