# Design

## Context

See proposal.md for the motivation and specs/ for the behaviour. Current state
that shapes the approach:

- `Reminders.claim_due` (`reminders/store.py`) marks a due reminder `fired` and
  inserts one `queue_items` row per recipient in one transaction. The queue
  item has `reminder_id` but no due time. `Worker._reminder_turn` (`queue.py`)
  reads text, due time and creator back from the `reminders` row.
- `History.recent` (`history.py`) labels a reminder entry with
  `Reminder.due_at`, joined through `entries.reminder_id`. That is only right
  while a reminder fires once.
- `REMINDER_RULE` (`agent/prompts.py`) tells the model not to act with tools.
  Reminder turns are already offered the web, MCP, calculator and date tools;
  every alfred toolset that reads or writes user data is `chat_only`.
- `Worker._complete` replaces a blank or `NO_REPLY` reminder answer with the
  fixed notice. For `/ask`, `NO_REPLY` records nothing and sends nothing.
- `reminders/when.py` builds local times with `_local(day, clock, zone)`:
  `fold=0`, and a time in a DST gap moved forward by the gap.
- Idle close (`idle.py`, `History.close_idle`) only looks at conversations
  with a `user` entry. `History.append` takes the current conversation with
  `FOR SHARE`, which `close_idle` and `reset` wait for. The idle marker is
  `end_reason = 'idle' AND marker_sent_at IS NULL`; its "new conversation"
  id is the user's current one when the marker is claimed.
- The sweeper summarises every archived conversation and wakes on
  `Summaries.changed`. Fact extraction finishes a conversation with no user
  message without a model request.

## Goals / Non-Goals

**Goals:**
- One reminder concept: a one-shot reminder is a reminder with no rule.
- Occurrences are computed from the rule each time, never by adding 24 h.
- Everything stays in the database: no timers, and restarts are safe.

**Non-Goals:**
- Changing the due loop's timing or the queue's ordering.
- Tools beyond `alfred_reminders_list` in reminder turns (memory search and
  facts are a later change if wanted).
- Changing an existing series in place: cancel and set again.

## Decisions

### D1. Data model (migration `0008`)

On `reminders`:

- `repeat text NULL`, CHECK in `daily`, `weekdays`, `weekends`, `weekly`.
  `NULL` is a one-shot reminder.
- `repeat_days smallint NULL`: a bitmask, Monday = bit 0 … Sunday = bit 6,
  set for every rule (`daily` = 127, `weekdays` = 31, `weekends` = 96, `weekly`
  = the given days). The next-occurrence code only reads the mask; `repeat`
  stays for showing the rule ("every weekday").
- `repeat_time text NULL`: the local wall time `HH:MM`. A UTC `due_at` can't
  give the wall time back across a DST change, so it is stored.
- `quiet_ok boolean NOT NULL DEFAULT false`.
- CHECK: `repeat`, `repeat_days` and `repeat_time` are all null or all set.

`reminder_due_at timestamptz NULL` on `queue_items` and on `entries`: the due
time of the occurrence the item delivers or the entry records. The migration
backfills both from `reminders.due_at` for existing reminder rows, which is
exact because every existing reminder is one-shot.

`END_REASONS` gains `unattended` (D7).

- *Alternative:* a `schedules` table that creates a one-shot reminder row per
  occurrence. That gives "skip just tomorrow's" and per-occurrence numbers,
  but means two tables, two numbers for one thing in the list, and a generator
  loop. Rejected for now. Skipping one occurrence can be added later as a
  column.
- *Alternative:* store `repeat_days` as text (`"tue,fri"`). A mask needs no
  parsing and its CHECK is a range.

### D2. Next occurrence

`when.py` gains:

```
next_occurrence(days_mask, clock, after, zone) -> datetime
    for each local date from after's local date, for 8 days:
        if the date's weekday is in the mask:
            candidate = _local(date, clock, zone)
            if candidate > after: return candidate
```

Eight days covers a weekly rule whose only day is today and has passed.
`_local` already handles gaps and folds, so DST needs no new code. The zone is
the one configured now: changing `[ai] timezone` moves the series from its
next occurrence on, the way a person would expect.

Parsing for `alfred_remind_set`: `repeat`, `days` (accepting `mon`…`sun`, full
names, any case) and `at`, which must match `HH:MM` when `repeat` is given.
Errors are `WhenError`s with the accepted forms, like the existing ones. The
first occurrence is `next_occurrence(mask, at, now, zone)`, so a time still
ahead today counts.

`format_rule(repeat, mask, clock)` gives "every day at 08:00", "every weekday
at 08:00", "every weekend day at 10:00" and "every Tuesday and Friday at
20:00". It is used in the set result, the list and the reminder input.

### D3. Claiming a repeating reminder

`claim_due` keeps its one transaction and `FOR UPDATE SKIP LOCKED`. Per due
reminder:

- **One-shot:** as today, plus `reminder_due_at = due_at` on each queue item.
- **Repeating:** the occurrence delivered is the latest occurrence at or before
  `now`, found by stepping `next_occurrence` from `due_at` while the next one
  is still `<= now`. On time, that is `due_at` itself; after an outage it is
  the most recent missed one. Each queue item gets that as `reminder_due_at`.
  Then `due_at = next_occurrence(…, after=now)`, `fired_at = now`, and the
  status stays `pending`.

Stepping is bounded by the outage length (a year down is 365 steps of a
daily rule) and only happens on a late claim. `Reminders` needs the zone for
this, so it gets the zone in its constructor.

- *Alternative:* a separate `previous_occurrence`. That would be a second
  function to keep consistent with `next_occurrence`, for a rare path.

### D4. Tools

`alfred_remind_set` gains `repeat` (enum), `days` (array of enum weekday
names), and `quiet_ok` (boolean). The descriptions do the routing work for a
small model:

- `repeat`: "For 'every day', 'every weekday', 'at weekends' or 'every Tuesday
  and Friday'. Needs at as HH:MM."
- `quiet_ok`: "true when the user only wants to hear if there is something to
  tell ('tell me if…', 'only if…')."

The function description adds that the reminder may ask alfred to do
something at that time ("brief me on the weather") and that it is not done
now.

The set result for a repeating reminder: `Reminder set, every weekday at
08:00, starting tomorrow at 08:00: brief me …`, with the same "confirm in your
own words, no number" tail.

The list line for a repeating reminder: `#31 Monday 16 November 08:00 (in
12 h), every weekday at 08:00, may stay quiet: brief me …`.

**Offering:** `ReminderTools.open` returns itself on chat turns, a list-only
view on reminder turns (definitions filtered to `alfred_reminders_list`), and
`None` otherwise. One class keeps the store and formatting together, and
tools are offered in the same order. `Gated` still applies the recipient's
allow-list. Setting and cancelling stay out of reminder turns, so a series
can't create or cancel series.

- *Alternative:* two `LocalToolset` classes. That's more wiring in
  `build_toolsets` for the same effect.

### D5. Cancelling per recipient

`Reminders.cancel` locks the reminder as today, then:

- turn user is the creator: status `cancelled` for everyone (as today);
- else, a recipient: delete their `reminder_recipients` row; if none remain,
  status `cancelled`. New outcome `left`, returning the remaining recipients.

Items already queued are not touched: they were due before the cancel. Past
history entries keep `reminder_id`, and `created_by` still comes from the row,
so "set by diogo" stays right in old entries.

Result for `left`: `You won't be reminded of #15 (… ): dinner …; it stays set
for diogo.`

### D6. Reminder turns

`TurnInput` gains `repeat_label: str | None` and `quiet_ok: bool`, filled by
`Worker._reminder_turn` from the row, with `due_at` from
`item.reminder_due_at`.

The input becomes `[reminder:#31 due Tuesday 17 November 2026, 08:00; now
08:00; every weekday at 08:00] brief me …` (the rule only when it repeats).

`REMINDER_RULE` is rewritten:

> This turn was started by a reminder set earlier, by the user or by the user
> named after "set by", shown in the last message with when it was due and the
> time now. If the reminder asks you to find out, check or sum up something,
> do it now with your tools, then write the message; otherwise just remind the
> user. It will be sent to them as is. If it is late, say how late. If someone
> else set it, say who. Don't mention the reminder's number.

Followed by one of:

- not quiet: "Always write a message: the user must receive it."
- quiet: "If there is nothing worth telling, answer exactly NO_REPLY and
  nothing else."

`system_prompt` takes `reminder_quiet: bool` to pick the line.

In `Worker._complete`, a reminder item whose answer is `NO_REPLY` and whose
turn is quiet is logged at INFO and finished with nothing recorded or sent,
like `/ask`. A blank answer or `NO_REPLY` without `quiet_ok` still gets the
fixed notice, and so does a failed turn, quiet or not (`_turn_failed` is unchanged).
That way a broken AI doesn't look like "nothing to report".

The routing rule gains: "A request to do something later or on a schedule
('every weekday at 8, brief me on the weather') is a reminder too, even
without the word 'remind': set it, don't do it now. 'Every …' takes repeat
(and days for given weekdays) with at as HH:MM; 'only if …' or 'tell me if …'
takes quiet_ok."

### D7. History: entry due time and unattended close

`History.append` takes `reminder_due_at` and stores it on the entry.
`_deliver_reminder` passes the turn's due time on both the `reminder` and the
`reminder_reply` entry. `recent` reads `Entry.reminder_due_at` and keeps the
join to `reminders` only for `created_by`.

**Unattended close.** `History` gets the idle close time (or `None`) and an
`on_archive` callback (the sweeper nudge). In `append`, for kinds `notice`,
`ask_prompt` and `reminder`, which are the first entry of what arrives, never
the reply, the current conversation is taken `FOR UPDATE` instead of
`FOR SHARE`, and in the same transaction:

```
if idle_after is set
   and no 'user' entry in the conversation
   and the newest entry's created_at < now - idle_after:
       archive it (ended_at = now, end_reason = 'unattended')
       start a new conversation for the user
       insert the entry there
```

Then `on_archive()` runs after commit. The reply entry that follows
(`ask_reply`, `reminder_reply`) goes to the current conversation, the new one,
so the pair stays together.

- **No queue check,** unlike `close_idle`. The item doing the append is itself
  pending. A user message waiting behind it hasn't been appended yet and will
  land in the new conversation, together with the briefing it probably
  answers.
- **Retries.** An entry already recorded for the queue item is returned
  before the check runs, so a retried reminder never closes the conversation
  it was recorded in.
- **No marker.** `unattended` is not `idle`, so `take_idle_marker` never
  hands it out. An earlier `idle` marker still names the user's current
  conversation when it is claimed, as the spec scenario requires.
- **Concurrency.** A `/notify` notice and a reminder for the same user at the
  same moment are serialised by `FOR UPDATE`. The second sees a conversation
  whose newest entry is the first one, so there is no second close.

- *Alternative:* a timer in `IdleCloser` from the last entry. Rejected in the
  explore session: it drops the briefing before a late reply and sends a
  marker almost every day.

### D8. Wiring

`app.py` passes the zone to `Reminders`, and the idle close time and the
sweeper nudge to `History`. `tools/catalog.py` needs nothing: no new tool
names. No new config.

## Risks / Trade-offs

- **[A small model runs the briefing now instead of scheduling it]** → The
  routing rule and the `alfred_remind_set` description both say it. Add a
  turn test with a scripted model, and an end-to-end check on `ai.lan`
  (tasks).
- **[A reminder set by someone else acts with the recipient's tools]** Diogo
  can set "every day, add X to my Trello" for Ana, and it runs with Ana's
  allow-list. The answer still goes only to Ana, her data stays hers, and
  her allow-list bounds what can run. This is accepted for a household
  bot. If it's ever not acceptable, reminders set by someone else can get
  the old "don't act" instruction.
- **[A reminder turn with tools is no longer cheap]** A briefing makes several
  tool calls and model rounds every weekday. One per occurrence, through the
  single queue, so it never runs alongside a user's turn.
- **[Side effects on retry]** A reminder turn that ran tools and then failed is
  not repeated (message-queue "No repeat after tools ran"), and the fixed notice
  is sent. Same as user turns.
- **[One summary request per unattended conversation]** About one a day per
  briefing user, only when the queue is quiet. It is what makes "what did
  Tuesday's briefing say" searchable.
- **[A late occurrence after a long outage is stale]** An 08:00 briefing
  delivered at 19:40 says it's late. A quiet one may stay quiet. Dropping
  stale occurrences is out of scope.
- **[Recipient leaving changes a one-shot behaviour]** Recorded as BREAKING in
  the proposal. The old "Ana cancels, nobody gets it" scenario is replaced.

## Migration Plan

- Migration `0008` adds the columns, the CHECKs and the `unattended` end reason,
  and backfills `reminder_due_at`. Existing reminders stay one-shot and
  behave as before.
- Downgrade: cancel pending repeating reminders, drop the new columns, and
  turn `unattended` conversations into `idle` with `marker_sent_at` set, so no
  marker appears.
- Release: bump the version, build and push the image, write a handover update
  (migration `0008`, no new config).
