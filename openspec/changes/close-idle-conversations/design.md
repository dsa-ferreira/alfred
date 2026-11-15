# Design

## Context

See proposal.md for the motivation. The relevant current state:

- `Worker._complete` (`queue.py`) calls `_reset` after `alfred_end_conversation`.
  `_reset` runs `History.reset`, then awaits `Summaries.summarise` on the
  archived conversation, then sends the marker. All of this happens inside the
  single queue worker, so the next item waits for it.
- `Sweeper` (`memory/sweeper.py`) is its own task. It summarises and embeds
  every archived conversation that needs it, oldest first, honours
  `summary_next_at`, and wakes on `Summaries.changed`. It already backfills, so
  conversations archived without a summary are picked up with no new code.
- A queue item stays `pending` from enqueue until `Queue.finish`, including
  while its turn runs. "Waiting or running" is therefore one indexed query
  (`ix_queue_items_pending`).
- `Queue.signal` is the worker's wake-up event. The worker clears it, and the
  reminders loop sets it directly (`run_due_loop(..., queue.signal, ...)`).
- A user's message is enqueued first. Its `user` entry is appended only when
  its turn starts, inside a transaction that holds `FOR SHARE` on the current
  conversation row. `History.reset`'s UPDATE waits for that lock.
- The summary and the chat turn both go to Ollama on `ai.lan`, which runs one
  request at a time. `ModelClient.complete` sends `"stream": false`.
- Measured on `ai.lan` (Ollama 0.40.13, `bonsai-2:27b`, 14 Nov 2026), by
  dropping a long request and timing a tiny one sent right after (0.2 s when
  idle):
  - non-streamed, dropped while generating: the tiny request waited 25.5 s,
    the rest of the long request's 30 s. Ollama kept going.
  - streamed, dropped while generating: 1.2 s. Ollama stopped.
  - streamed, dropped 2 s into reading a 60,000-character prompt (about
    8.7 s to read): 7.9 s. Reading the prompt finished, then Ollama stopped
    before generating. Non-streamed: 13.7 s, so it read and generated.
  - streamed replies report token counts with
    `stream_options: {"include_usage": true}`.

## Goals / Non-Goals

**Goals:**
- The queue worker never waits for a summary.
- A summary request never holds the model server while a turn wants it.
- Idle close survives restarts without keeping timers in memory: everything
  it needs is in the database.

**Non-Goals:**
- Preempting the embedding request (ragman, a different server) or the
  database writes around it.
- Several alfred processes. The queue-activity signal (D5) is in-process,
  like `Queue.signal` today.
- Telling the model about the previous conversation after an idle close.

## Decisions

### D1. Resets only archive; the sweeper does every summary

`_reset` runs `History.reset`, sets `Summaries.changed` so the sweeper looks
right away, and sends `reset_marker(archived_id, new_id)`. The worker no
longer needs a `Summariser`. Its `memory` argument becomes a nudge callable
(`summaries.changed.set`), or `None` in tests. `reset_marker` loses `title`
and gains `idle: bool`, which picks between `archived` and
`closed after inactivity`.

- *Why:* the sweeper already handles retries, backfill and the per-conversation
  lock. Two paths that both summarise were only needed to put the title in the
  marker.
- *Alternative:* keep the worker call but spawn it as a task. Rejected: it
  still competes for the model server (D5 and D6 fix that only in one place),
  and it duplicates the sweeper.

### D2. How a conversation ended is stored on the conversation

Migration `0006` adds two nullable columns to `conversations`:

- `end_reason text` with CHECK `end_reason IN ('user', 'idle')`. `History.reset`
  sets `'user'`, idle close sets `'idle'`. Older archived rows stay `NULL`.
- `marker_sent_at timestamptz`: set when the idle marker is handed out (D4).

A pending idle marker is `end_reason = 'idle' AND marker_sent_at IS NULL` on
one of the user's archived conversations.

- *Why:* the marker has to survive a restart between the close and the next
  message, and the conversation row is where its facts already live.
  `end_reason` is also useful in logs and later queries.
- *Alternative:* a separate "pending notices" table. Rejected as a new concept
  for a single use.

### D3. Idle close is its own background task, scheduled from the database

A new `IdleCloser` (`src/alfred/idle.py`), started in `app.py` only when
`[history] idle_close_after_h` is set. Its loop:

1. Find candidates: current conversations whose newest `user` entry is older
   than `now - idle_close_after`. Conversations with no `user` entry are never
   candidates.
2. For each candidate, call `History.close_idle(conversation_id, cutoff)`.
3. Sleep until the soonest future due time (newest `user` entry plus
   `idle_close_after` over all current conversations), or for
   `idle_close_after` when there is none. If a candidate was skipped because
   of a queue item, sleep at most 60 seconds instead.

The sleep is never too long: a new message can only push a conversation's due
time later. A conversation that gets its first user message after the
computation is due no sooner than `now + idle_close_after`, which is the
longest the loop sleeps. No wake-up event is needed.

`History.close_idle`, in one transaction:

1. `SELECT ... FOR UPDATE` on the conversation row, `ended_at IS NULL`. This
   waits for any in-flight `append`, which holds `FOR SHARE`.
2. Re-check, with new statements so they see what just committed: the newest
   `user` entry is still older than the cutoff, and the user has no `pending`
   queue item.
3. Archive it with `end_reason = 'idle'`, insert the new current conversation,
   and return both ids. If a check fails, return `None` and change nothing.

Then set `Summaries.changed` and log at INFO.

- *Why the lock comes first:* under READ COMMITTED, a single
  `UPDATE ... WHERE NOT EXISTS (...)` re-checks the row but not its subqueries
  after waiting for a lock. A message whose `user` entry committed during the
  wait could end up in the archived conversation, with its reply in the new
  one. Locking first and then checking avoids that.
- *A message enqueued after the check* lands in the new conversation, and its
  turn sends the marker first. That is the intended result, since the
  conversation really was idle.
- *Alternative:* close lazily when the user next writes (option D in explore).
  Rejected by the user: the summary would then run while they wait.

### D4. The idle marker is sent when the user's next message starts

In `Worker._process`, for `kind == "user"` items, after the user check and
before the `user` entry is appended, the worker calls
`History.take_idle_marker(user)`. In one transaction it finds the user's
newest pending idle marker, sets `marker_sent_at = now()` on it (and on any
older pending ones), and returns `(archived_id, current_id)`. The worker then
sends `reset_marker(archived_id, current_id, idle=True)`.

It is claimed before it is sent, so a retried turn or a crash never sends it
twice. A failed send is logged and not repeated, like every other notice
(`_send`). `/notify`, `/ask` and reminder items don't take the marker.

### D5. A queue-activity signal for the sweeper

A small `QueueActivity` object, owned by `Queue`:

- `notify()` sets the worker's `signal` and an `arrived` event. Every enqueue
  path calls it, including the reminders loop, which gets the `Queue` (or
  `notify`) in place of the bare `signal` event.
- `finished()` is called by the worker after each item ends, whatever the
  outcome. It records `last_finished` (monotonic) and sets a `changed` event.
- `last_finished` starts at process start, so after a restart summaries wait
  one quiet period too.

Before starting a summary request, the sweeper waits until both hold: no
`pending` queue item (DB query) and `summary_quiet_s` has passed since
`last_finished`. While items are pending it waits on `changed`. Otherwise it
sleeps for the time left in the quiet period, and wakes early on `changed`.
A conversation that only needs its embedding doesn't wait.

- *Why in-memory:* the queue and the sweeper share a process, and "finished
  recently" doesn't need to survive a restart.
- *Alternative:* the newest `finished_at` from `queue_items`. That column has
  no index, and the table grows forever.

### D6. Preempting the summary request

`Summaries.summarise(conversation_id, preempt: asyncio.Event | None = None)`.
Only `_make_summary` uses `preempt`: it runs `model.complete(...)` as a task
and waits for whichever comes first, the task or `preempt.wait()`. If
`preempt` wins, it cancels the task, awaits it, and returns a `PREEMPTED`
result. `summarise` then returns without calling `_retry_later`, so
`summary_attempts` and `summary_next_at` don't change. The embedding and the
database writes are outside the race.

The sweeper clears `activity.arrived` after its quiet check and before it
calls `summarise(due, preempt=activity.arrived)`. An arrival after the clear
sets it, and an arrival before the clear was already seen by the quiet check
as a pending item. After a preemption the sweeper goes back to waiting for a
quiet queue (D5). It must not treat "still due right after processing" as a
failed retry write (the `due == last` branch in `run_forever`). A preempted
pass resets `last`.

The summary request is **streamed**. `ModelClient` gets a streaming
variant of `complete` (`complete_streamed`) that sends `"stream": true` with
`stream_options: {"include_usage": true}`, reads the SSE chunks, and returns
the same `ModelReply` (joined content, usage from the final chunk). It uses
the same error classes. Only `Summaries` uses it, and chat turns keep
`complete`. Cancelling the task makes httpx close the connection, and Ollama
stops at its next token.

- *Why streaming:* measured (see Context). Ollama ignores a dropped client on
  a non-streamed request until it has generated the whole reply.
- *Limit:* a disconnect while Ollama is still reading the prompt only takes
  effect once reading is done. A message can therefore wait for the rest of
  the summary's prompt reading, up to about 9 s at
  `summary_input_max_chars = 60000`. Lowering that cap shortens the
  worst case.
- *Alternative:* switch chat turns to streaming too. Not needed for this
  change.

### D7. Config keys

- `[history] idle_close_after_h: PositiveFloat | None = None`. Hours, because
  the useful range is hours. `None` means off, and `0` is rejected.
- `[memory] summary_quiet_s: NonNegativeFloat = 60`. `0` means "wait only for
  an empty queue". The default is a placeholder, and the owner picks the value
  for their deployment (see Open Questions).

## Risks / Trade-offs

- [A message arriving while a summary's prompt is still being read waits
  for the reading to finish (up to about 9 s at the default input cap)] →
  Accepted. The quiet-queue wait (D5) makes this rare, and
  `summary_input_max_chars` bounds it.
- [A future Ollama version changes how it handles a dropped client] → Task
  5.2 repeats the measurement through alfred itself. Re-run it after Ollama
  upgrades if summaries seem to hold up turns.
- [Repeated preemption: a user who chats all day delays summaries for hours]
  → Accepted. Summaries aren't urgent, keyword and date search still find the
  conversation, and they catch up at the first quiet stretch.
- [Wasted prompt processing: a cancelled long transcript is processed again
  next time] → Accepted. It only costs idle GPU time.
- [Idle close splits a thread the user meant to continue the next morning] →
  The marker makes it visible, and recall brings it back.
- [The marker no longer shows the title] → Accepted by the owner. The title
  shows in search results.
- [In-process activity signal] → Matches the existing single-process design
  (`Queue.signal`).

## Migration Plan

1. Migration `0006_conversation_end_reason`: add `end_reason` (with CHECK)
   and `marker_sent_at`, both nullable. No backfill. Downgrade drops them.
2. Deploy as usual (`alfred migrate` runs first). With
   `idle_close_after_h` unset, the only visible change is the marker without
   a title and summaries giving way to turns.
3. The owner sets `idle_close_after_h` and, if wanted, `summary_quiet_s`.
4. Rollback: the previous image ignores the new columns. Conversations that
   were idle-closed stay archived and summarised.

## Open Questions

- The value of `summary_quiet_s` (placeholder 60) and the owner's
  `idle_close_after_h`. Both are config, so they can be tuned after deploy
  without changing the design.
