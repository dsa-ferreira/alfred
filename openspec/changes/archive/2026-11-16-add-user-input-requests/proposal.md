# Proposal

## Why

The model runs tools that change things (`mcparr_add_media`, cookbook's
create and update tools) as soon as it decides to, and nobody can stop a
call before it runs. Some users and some tools need a "yes, do it" from a
person first.

MCP servers can now ask the user something in the middle of a tool call:
protocol 2026-07-28, which alfred already negotiates, has a server answer a
call with `InputRequiredResult`. alfred's client doesn't declare that it can
show such questions, so a server that asks fails the whole call.

Both cases need the same thing: a turn that stops and waits for the user's
answer, without blocking the single queue worker, and then goes on as if the
tool call had just returned. OpenJarvis's approval queue (pending actions,
expiry) was the reference. Its remembered "always approve" decisions are left
out.

## What Changes

- **Confirm list per user.** `[[users]]` gets a `confirm` list in the same
  syntax as `tools` (exact names, `prefix*`, `*`). A call to a tool it matches
  doesn't run. The turn waits instead, and alfred sends the user a Telegram
  message showing the tool and its exact arguments, written by alfred and not
  by the model, with **Do it** and **Cancel** buttons.
- **Waiting turns.** A turn that needs an answer is parked: its queue item
  stops blocking the queue, and other items and users go ahead. When the user
  taps a button, the turn resumes from that tool call. The tool's real result
  (or "declined by the user") goes to the model, which carries on and answers
  as usual. One question is open per turn at a time.
- **Server questions (MCP elicitation).** alfred declares form elicitation.
  When a server answers a call with `InputRequiredResult`, alfred parks the
  turn and stores the server's opaque `request_state`. It shows the server's
  message as buttons, then repeats the call with the user's answer. alfred can
  only show questions that fit buttons: an empty form (yes/no), one boolean,
  or one choice from a list. Any other question (text fields, URL mode,
  sampling, roots) makes the call fail with an error the model sees, and
  nothing is sent to the server.
- **Ending a wait without an answer.**
  - **Cancel** declines the request, and the model is told so.
  - **A new message** from the user cancels all of their waiting turns. No
    reply is made for them, and the conversation records that the action was
    not done.
  - **Expiry**, after a configurable time, does the same.
  - **A server that rejects a late answer** (expired or unknown
    `request_state`) gives the model an error saying the approval could not be
    used.
- **Safety.** Only the user whose turn it is can answer, in their private
  chat. An answer is applied at most once: double taps and taps on old
  messages are refused. A call is marked as running before it starts, so a
  crash never runs it again. Waiting turns survive a restart.
- **All turn kinds.** Chat, `/ask` and reminder turns can all wait. The
  question message stands on its own and says what asked: the user's request,
  the service, or the reminder.
- **Telegram.** alfred receives callback queries (button taps) for the first
  time. When a request ends, alfred edits the question message to show the
  outcome and removes its buttons.

## Capabilities

### New Capabilities

- `user-input-requests`: the per-user confirm list; parking and resuming
  turns; question messages and buttons; answers, cancellation, expiry,
  at-most-once; MCP elicitation forms, and what alfred can't show; how waiting
  turns are recorded in the conversation.

### Modified Capabilities

- `message-queue`: "Single ordered worker". A waiting item doesn't block the
  queue, and a resumed item runs before items queued after it. A new
  requirement for waiting items surviving restarts and never applying an
  answer twice.
- `mcp-tools`: a new requirement. alfred declares form elicitation, and
  parks or refuses a server's `InputRequiredResult`.
- `users`: a new requirement for the per-user `confirm` list.
- `service-configuration`: "Users in the config file" (the `confirm` key and
  its validation), and the new `[input_requests]` settings in the list of
  settings.
- `telegram-chat`: "Reply to every owner message" (the reply to a waiting
  turn comes after the answer, or not at all if it is cancelled), "Typing
  indicator while an owner turn runs" (not shown while waiting), and a new
  requirement for buttons and taps.

## Impact

- **Code:**
  - `tools/base.py`: a new "needs input" outcome, a `resume` method on open
    toolsets, and a `Confirmed` wrapper next to `Gated`.
  - `agent/turn.py`: parking and resuming a turn.
  - New package `alfred/inputs/`: the store, question rendering, the
    callback handler and the expiry loop.
  - `mcp/client.py` and `mcp/registry.py`: the elicitation capability, the
    session-level call, and resuming with `request_state`.
  - `queue.py`: the `waiting` status, resume order, and cancelling on a new
    message.
  - `history.py`: idle close also waits for `waiting` items.
  - `telegram/bot.py`: callback queries, and `callback_query` in
    `ALLOWED_UPDATES`.
  - `config.py`: `confirm` and `[input_requests]`.
  - `notices.py`, `app.py`, and `alfred.toml.example`.
- **Database:** migration `0010`, with an `input_requests` table and a
  `waiting` value for `queue_items.status`.
- **MCP servers:** no change needed. cookbook and mcparr don't ask for input
  today. When they do, the SDK's default `request_state` key gives answers
  600 s and is lost on a server restart. A shared key and a longer TTL
  (`RequestStateSecurity`) are a later, separate change in those repos.
- **Handover:** an update for the release (migration `0010`, new config
  keys).
- **Out of scope:**
  - Remembered "always allow/deny" decisions.
  - A model tool for asking the user to choose (`alfred_ask_user`). It will
    reuse this mechanism later.
  - Free-text and URL-mode elicitation, sampling and roots requests.
  - Several open questions in one turn.
  - Per-server request TTL settings.
