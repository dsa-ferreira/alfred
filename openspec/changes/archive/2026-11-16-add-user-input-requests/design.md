# Design

## Context

See proposal.md for the motivation and specs/ for the behaviour. The current
state that shapes the approach:

- **One worker** (`queue.py`) runs the oldest `pending` queue item to the end.
  `Agent.run` (`agent/turn.py`) builds the context:
  - the system prompt;
  - `History.recent` of the user's current conversation;
  - the ask or reminder input.

  It then loops model -> tool calls -> model. Tool messages live only in
  memory during the turn; the history keeps only user and assistant text.
  Each call is stored in `turn_tool_calls`.
- **Toolsets** (`tools/base.py`): `turn.py` wraps every opened toolset in
  `Gated(open_toolset, user.tools)`. `dispatch` routes a call to the first
  toolset that `handles` the name. Any call sets `side_effects`, so the turn
  is never retried.
- **MCP calls** (`mcp/registry.py` `call_tool`) open a fresh connection for
  each call and use the high-level `Client.call_tool`. The `Client` is built
  without an `elicitation_callback` (`mcp/client.py`), so it declares no
  elicitation. A server's `InputRequiredResult` would go to the SDK's default
  callback and fail the call.
- **The SDK spike** (`mcp` 2.3.0, recorded in the explore session):
  - Against `MCPServer`, `Client` negotiates `2026-07-28`.
  - `client.session.call_tool(..., allow_input_required=True)` returns the
    `InputRequiredResult`.
  - The call resumes on a *fresh* connection with
    `input_responses={key: ElicitResult(...)}` and the opaque `request_state`.
  - The server refuses with `-32602 Invalid or expired requestState` when the
    TTL has passed (default 600 s), after a server restart (the default key is
    process-local), or when the arguments differ.
  - **The same `request_state` can be replayed, and the tool then runs
    again.**
  - A client without `elicitation_callback` gets "did not declare the form
    elicitation capability".
- **Telegram:**
  - `ALLOWED_UPDATES = ["message"]`.
  - `TelegramOutbox.send` sends formatted text and returns nothing.
  - The aiogram version is 3.31.
- **Idle close** (`History.close_idle`) skips a user who has a `pending`
  queue item.
- **Unattended conversations:** a conversation with no user message is
  archived when a new notice, ask or reminder arrives after the idle time
  (`History.append`).

## Goals / Non-Goals

**Goals:**
- A tool call can wait for the user without blocking the worker. Resuming
  looks like the call returning late, so the turn loop's logic stays as it is.
- One mechanism for alfred's confirm list and for MCP elicitation, ready for
  a later `alfred_ask_user`.
- Everything lives in the database. Taps, cancellation and expiry are atomic
  status changes, and nothing is applied twice.
- `turn.py` still names no tool. `tools/base.py` still imports nothing else
  from alfred.

**Non-Goals:**
- Changing how non-waiting turns run, retry or record.
- Free-text answers (they would need "the next message is the answer").
- Keeping the turn's brain: a resumed turn uses the brain current when it
  resumes.

## Decisions

### D1. Parking: the turn stops, the worker moves on

A tool call can return a **need for input** instead of a result. The turn then
stops at that call and is saved. Its queue item becomes `waiting`, so `head()`
(oldest `pending`) skips it. A tap makes the item `pending` again. Its id is
older than anything queued since, so it is the next head once the running
item ends. This is the "answered turn goes first" scenario, with no extra
ordering code.

Alternatives considered:
- **Blocking the turn in memory** until the tap. This blocks every user, and a
  restart loses the turn.
- **Ending the turn and running the call later as a "deferred action".** The
  model never sees the result, so tasks with several steps break at the first
  confirmation.

### D2. Toolset contract (`tools/base.py`)

- **`ToolOutcome.need: InputNeed | None`.**
  - `InputNeed` holds a neutral `Question`: `title` (who asks, filled in by
    the worker), `body` (text), `detail` (preformatted text, such as the
    arguments as JSON), and `options` (a list of `Option(label, answer)`).
  - It also holds `state`, a JSON-able dict the toolset needs to resume.
  - `ok` and `text` are unused when `need` is set.
- **`OpenToolset.resume(name, raw_arguments, state, answer, attempt) ->
  ToolOutcome`.**
  - `answer` is the chosen `Option.answer`, which is JSON-able.
  - `LocalToolset.resume` raises `NotImplementedError`, which `safe_call`
    turns into an error result.
  - `dispatch_resume(opened, name, ...)` routes it like `dispatch`.
- **`Gated.resume`** checks `handles(name)` first. The allow-list may have
  changed in the config while the turn waited; then the result is "unknown
  tool".
- **`Confirmed(inner, confirm: AllowList)`**, the outermost wrapper:
  `Confirmed(Gated(t, user.tools), user.confirm)`.
  - `call(name, args)`: for a matching name, if the arguments parse it returns
    `InputNeed(state={"via": "confirm"}, question=...)`. The question has the
    tool name, the parsed arguments as indented JSON, and the options
    **Do it** (`true`) and **Cancel** (`false`). Arguments that don't parse
    go straight to `inner.call`, which already returns the error without
    running anything: there is nothing to confirm.
  - `resume(...)` with `state["via"] == "confirm"`:
    - `true` calls `inner.call(name, args, attempt)`;
    - `false` returns `ToolOutcome(False, "The user declined this call; it
      did not run. Don't call it again unless they ask.")`.
  - Any other state goes to `inner.resume`, so an MCP question passes
    through.
  - It reuses `AllowList`, so the syntax is the same as `tools`.

Alternatives considered:
- **A confirm check inside `turn.py`.** It would make the loop name a policy,
  where `Gated` already sets the pattern.
- **A per-server `confirm_tools`.** It can't cover alfred's own tools, and
  trust is per user (decided in explore).

### D3. Saving and resuming a turn (`agent/turn.py`)

**Park.** In `_attempt`, when the k-th call of a model reply returns a need,
the loop stops and returns `TurnResult(waiting=Waiting(...))`, where
`Waiting` holds:
- `need`, the call (`call_id`, `name`, `arguments`) and `call_index = k`;
- `tail`: the messages after the context (`state.messages[context_len:]`).
  These are the assistant messages with `tool_calls` and the tool results so
  far, including calls 0..k-1 of this reply;
- `requests_used` (n) and `seq` (the tool-call counter);
- `conversation_id` and `upto_entry_id`: the newest entry the context was
  built from.

Calls k+1.. of the same reply are not run yet. They are in the saved
assistant message and run after the answer.

**Resume.** `TurnInput.resume: Resume | None` carries the saved `Waiting`
plus the answer. `_attempt` builds the context as usual, except:
- the history comes from `History.recent(user, limit, conversation_id=...,
  upto_entry_id=...)`, so it is the same conversation and the same entries
  as when the turn parked;
- the system prompt is rebuilt, with today's date and the current brain;
- the saved `tail` is appended.

The loop then:
1. runs `dispatch_resume` for the waiting call and appends its tool message;
2. runs calls k+1.. of that reply through `_run_tool` (any of them may park
   again: D1 applies, with one question open at a time);
3. continues the model loop, with `n` carried over so
   `max_requests_per_turn` counts the whole turn.

`side_effects` starts as `True`. `seq` continues, so `turn_tool_calls` has one
numbering per item. The resumed call is recorded like any call. A park is
recorded too, as `ok = false` with the result excerpt "waiting for the
user's answer".

**Other turns don't see a parked turn.** While a turn is parked, its
entries (a chat turn's user message) have no reply after them. Another
turn running meanwhile, such as an `/ask`, would read that message as an
open request and act on it: on 0.16.0 an `/ask` from mcparr about Sonarr
added the film the parked turn was asking about. So `History.recent` for a
turn leaves out the entries of every *other* queue item with a live
request (`open`, `answered` or `running`), before the window limit. Once
the parked turn ends, its entries show again with their reply or the
"not done" entry. The resumed turn reads its own context by
`upto_entry_id`, so it is not affected. A label telling the model not to
act on the message was rejected: it relies on a small model honouring it.

Images in the tail's tool messages are replaced by `[n images not kept while
waiting]`. This keeps the stored tail small, and the `ModelImage` data was
already seen by the model.

Alternatives considered:
- **Storing the whole message list.** It would include the system prompt and
  the inline attachment images, so rows would be large, and a stale prompt
  would be used after midnight.
- **Rebuilding from the *current* history.** The context would change under
  the turn. Small models handle that badly, and the explore session chose
  "a new message cancels" for the same reason.

### D4. Data model (migration `0010`)

`queue_items.status` gains `waiting` and `cancelled`.

New table `input_requests`:

| column | |
| --- | --- |
| `id bigint pk` | in `callback_data` |
| `queue_item_id bigint fk` | the parked item |
| `user_name text` | whose turn; every query filters on it |
| `status text` | `open`, `answered`, `running`, `done`, `cancelled`, `expired` (CHECK) |
| `tool text`, `arguments jsonb` | the model-facing call |
| `question jsonb` | the rendered `Question` (title, body, detail, options) |
| `state jsonb` | the toolset's resume state (for MCP: server, key, `request_state`) |
| `answer jsonb NULL` | the chosen option's answer |
| `turn jsonb` | `Waiting` without `need` (tail, call, counters, conversation, upto) |
| `telegram_message_id bigint NULL` | the question message, for edits |
| `cancel_reason text NULL` | `new_message` or `expired` |
| `created_at`, `expires_at`, `decided_at` | |

A partial unique index on `(queue_item_id) WHERE status IN ('open',
'answered', 'running')` allows one live request per turn.

### D5. Transitions, all single atomic `UPDATE ... WHERE status = ... RETURNING`

```
                 tap (owner, not expired)        worker picks item
  open ----------------------------------> answered ----------------> running --> done
   |  \                                     (item: waiting -> pending)   (turn ends)
   |   \-- user's new message --> cancelled
   \------ expiry loop / late tap --> expired       (item -> cancelled, both)
```

- **Tap.** In one transaction, the request goes `open -> answered`, filtered on
  `user_name` and `expires_at > now()`, and the item goes `waiting ->
  pending` with `next_attempt_at = now()`. Then `activity.notify()`.
  - A second tap matches no row. It is answered "no longer open" (spec: an
    answer is applied once). Case I of the spike makes this the only guard
    against a replay.
  - A tap after `expires_at` expires the request there and then, then answers
    "no longer open".
- **Worker.** Before running a `pending` item, the worker looks for its
  request:
  - `answered`: it goes `answered -> running`, and the turn resumes with that
    answer.
  - `running`: alfred stopped mid-resume. The item fails with
    `notices.failed(partial=True)` and is not run again.
  - When the resumed turn ends (reply, park again, or failure) the request
    becomes `done`.
- **New message.** `Queue.enqueue_user` (used by text messages and by the
  attachment intake) first calls `InputRequests.cancel_for(user,
  "new_message")`. That moves `open -> cancelled` for that user and moves
  their items `waiting -> cancelled`. Requests already `answered` are no
  longer waiting and go ahead.
- **Expiry.** `inputs/loop.py` checks every `[reminders] check_interval_s`
  (no new setting). It does `open AND expires_at <= now() -> expired`, the
  same ending as cancel. It runs on its own clock, like the reminder loop, so
  idle close is never held up past the expiry.
- **Ending without an answer** (cancel and expiry share one path):
  - The fixed entry is recorded by kind (spec "Cancelled turns in the
    conversation"):
    - chat: an `assistant` entry;
    - ask: `ask_prompt` + `ask_reply`;
    - reminder: `reminder` + `reminder_reply`.
  - The text comes from `notices.input_not_done(address, tool, reason)`.
  - Then the question message is edited. Edits are best-effort and logged.

### D6. MCP input requests (`mcp/client.py`, `mcp/registry.py`)

**Declaring the capability.** The connector passes
`elicitation_callback=_refuse` to `Client`. `_refuse` returns `ErrorData` and
is never meant to be reached; its presence is what declares form and URL
elicitation. We don't want URL mode, but the SDK declares both together. A
URL request is refused in the classifier below, which the spec allows.

**First call.** `call_tool` uses `client.session.call_tool(route.tool, args,
allow_input_required=True)`. An `InputRequiredResult` goes to
`classify(result)`:
- **Showable:** exactly one input request, an `ElicitRequest` in form mode,
  with a schema of:
  - no properties: options **Yes** -> `accept {}` and **No** -> `decline`;
  - one boolean property: **Yes** -> `accept {p: true}`, **No** -> `accept
    {p: false}`, **Cancel** -> `decline`;
  - one string property with `enum` (labels = values) or `oneOf` of
    `{const, title}` (labels = titles), at most 8: each -> `accept {p: v}`,
    plus **Cancel** -> `decline`.

  The result is `InputNeed(state={"via": "mcp", "key": k, "request_state": s,
  "round": 1})`. The question body is the server's `message`, the title is
  filled by the worker, and the detail is the tool name.
- **Anything else:** `ToolOutcome(False, "Error: <server> asked for input
  alfred can't show (<what>); the call did not complete.")`. Nothing is sent
  back.

**Resume.** `OpenMcpTools.resume` reconnects (a fresh connection, as for every
call). It calls `session.call_tool(route.tool, same args,
input_responses={key: ElicitResult(**answer)}, request_state=...,
allow_input_required=True)`.
- Another `InputRequiredResult` is classified again, with `round + 1`.
- Past 10 rounds (the SDK default) the result is an error.
- `MCPError` with code `-32602` becomes the "late answer" error result (spec
  "Late answers to a server").

The `answer` stored for an option is the `ElicitResult` as a dict, so the
resume has nothing to translate.

Alternatives considered:
- **Letting the high-level `Client.call_tool` drive the loop with a callback
  that waits for the tap.** It holds a connection and a task open for hours,
  and blocks the worker (D1).
- **A callback that raises.** It never sees `request_state`.

The SDK's `Client.call_tool` re-lists a tool once on `HEADER_MISMATCH` (a
`2026-07-28` connection that hasn't listed the tool). The session-level call
skips that recovery. Task 4.4 checks whether a fresh connection triggers it
over Streamable HTTP. If it does, `call_tool` re-lists and resends once, as
the SDK does.

### D7. Telegram (`telegram/bot.py`, `inputs/`)

**Updates.** `ALLOWED_UPDATES = ["message", "callback_query"]`.
`callback_data` is `ir:<request id>:<option index>`, at most 64 bytes. The
handler:
1. checks the sender is a configured user in a private chat;
2. calls `InputRequests.answer(id, index, user)`, which returns `answered`,
   `not_open` or `not_yours`;
3. always calls `callback.answer(text)`;
4. on `answered`, edits the message to the question plus "Confirmed" (or the
   chosen label), with no keyboard.

A `not_yours` tap is logged with the tapper's ID only.

**Outbox.** `TelegramOutbox` gains:
- `send_question(user, question) -> message_id`: HTML, `detail` in
  `<pre>`, escaped, `detail` cut to fit 4096 with a marker, and an
  `InlineKeyboardMarkup` with at most 2 buttons per row;
- `edit_question(user, message_id, text)`: `edit_message_text` with no reply
  markup; failures are logged.

The question title is set by the worker from the item:
- chat: "To do what you asked:";
- ask: "[jellyfin] asks to:";
- reminder: "Reminder #12 (call mum):";
- MCP: "mcparr asks:".

### D8. Worker (`queue.py`)

- **`_process`:** for an item with an `answered` request, it builds the turn
  with `resume`, which skips recording the user message again. A chat item's
  typing indicator shows as usual.
- **A result with `waiting`:**
  1. one transaction: the item goes `waiting`, and the request row is
     inserted with `expires_at = now + expire_after_min`;
  2. `send_question`, then the message id is stored;
  3. any brain fallback is told first, as today.

  If `send_question` fails, the request is cancelled at once (the user can't
  answer what they never saw). The cancel entry says so.
- **Unchanged:** `_complete` and `_turn_failed` already handle a resumed turn
  by its kind, which gives `NO_REPLY` for ask and quiet reminders, and the
  fixed reminder notice on failure.
- **History:** `History.close_idle` treats `waiting` like `pending` (a busy
  user).

### D9. Configuration

- `UserEntry.confirm: list[str] = []` resolves to `User.confirm: AllowList`.
- Validation reuses the `tools` entry checks: the prefix rule and that exact
  `alfred_` names exist.
- At startup, alfred warns about an entry the user's `tools` can never match.
  For an exact entry that is `tools.matches(entry)`; for a pattern,
  `tools.may_match_prefix(prefix)`.
- A new `[input_requests]` section with `expire_after_min: PositiveFloat =
  60`.
- `alfred.toml.example` documents both.
- The model gets no prompt note: the decline and error texts tell it what it
  needs.

### D10. Package layout

`alfred/inputs/`:
- `store.py`: `InputRequests`, with the D5 transitions and the cancel path;
- `render.py`: `Question` to Telegram HTML and keyboard, and the titles;
- `loop.py`: expiry;
- `callbacks.py`: the tap handler that the bot registers.

`Question`, `Option`, `InputNeed` and `Confirmed` live in `tools/base.py`,
because toolsets produce them.

## Risks / Trade-offs

- [The model writes text next to the tool call that parks, which the user
  never sees] → This is the same as today, where assistant text alongside
  tool calls is not sent. The question message says what is asked.
- [A small model retries a declined call] → The decline text says not to
  call it again unless asked, and each retry asks the user again, so nothing
  runs without consent.
- [Server TTL (600 s default) is shorter than alfred's expiry (60 min)] → A
  late answer gives the model a clear error, and it can ask again. Longer
  server TTLs are a server-side change (proposal Impact).
- [`request_state` can be replayed, and the server runs the tool again] →
  The request moves `open -> answered -> running` before the call, so one tap
  means at most one call (D5).
- [HEADER_MISMATCH on a fresh session-level call] → Checked in task 4.4, with
  a re-list and resend as the SDK does.
- [A waiting turn holds idle close until it ends] → It is bounded by the
  expiry loop.
- [The brain changes while waiting] → The current brain resumes. A fallback
  can't re-run the turn, because `side_effects` is `True`.
- [Tail size] → It is bounded by `tool_result_max_chars` × calls in a turn
  (≤ 12 requests), and images are dropped.
- [Telegram edit fails, e.g. the message was deleted] → Logged; state is in
  the database.

## Migration Plan

- **Migration `0010`:** add the statuses to the `queue_items` CHECK, and
  create `input_requests`.
- **Downgrade:** mark `waiting` and `cancelled` items `failed`, drop the table,
  restore the CHECK.
- **Config:** nothing required; the defaults keep today's behaviour (empty
  `confirm` lists). The handover gets a release update naming migration
  `0010` and the new optional keys.
- **Rollback:** an older image refuses to start on a newer schema only if
  migrations are checked. Downgrade first if rolling back.

## Open Questions

- The exact wording of question titles, button labels and the "not done"
  texts. These are fixed notices, so they're easy to change later.
