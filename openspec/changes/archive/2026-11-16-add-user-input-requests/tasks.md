# Tasks

## 1. Schema

- [x] 1.1 Add migration `0010_input_requests` and the models (design D4): `waiting` and `cancelled` in `QUEUE_STATUSES`, and the `input_requests` table with its status CHECK and the partial unique index on live requests per item. The downgrade marks `waiting`/`cancelled` items `failed` and drops the table. Verify in `tests/db/test_schema.py` on Postgres:
  - an item can be set `waiting` and `cancelled`;
  - a second `open` request for the same item is rejected, while one `open` and one `done` are accepted;
  - an unknown request status is rejected;
  - downgrade to 0009 turns a `waiting` item `failed`.

## 2. Configuration

- [x] 2.1 Add `confirm` to `UserEntry`/`User` as an `AllowList`, and the `[input_requests]` section with `expire_after_min` (default 60, positive) (design D9). Validate `confirm` entries with the `tools` entry rules, and warn at startup about entries the user's `tools` can never match. Verify in `tests/config/test_config.py`:
  - the defaults: empty `confirm`, 60 minutes;
  - `confirm = ["add_media"]` is an error naming the user and entry;
  - `confirm = ["alfred_nope"]` is an error;
  - `expire_after_min = 0` is an error naming `input_requests.expire_after_min`;
  - `confirm = ["cookbook_*"]` with no `cookbook_` tools entry logs the warning;
  - every scenario of "Per-user confirm list" and the changed "User list".
- [x] 2.2 Document `confirm` (in the `[[users]]` example) and `[input_requests]` in `alfred.toml.example`, and verify `tests/config/test_example.py` still loads it.

## 3. Toolset contract

- [x] 3.1 In `tools/base.py` add `Option`, `Question`, `InputNeed` and `ToolOutcome.need`. Add `resume` to `OpenToolset`, with `LocalToolset.resume` raising `NotImplementedError` and a `dispatch_resume` (design D2). Add `Gated.resume`, which refuses names it doesn't handle. Keep the module free of other alfred imports. Verify in `tests/tools/test_base.py`:
  - `dispatch_resume` to a local toolset gives an error result;
  - `Gated.resume` for a name the allow-list no longer matches gives "unknown tool" without calling the inner toolset.
- [x] 3.2 Add `Confirmed(inner, confirm)` (design D2). It returns an `InputNeed` (tool name, indented JSON arguments, **Do it**/**Cancel**) for matching names with parseable arguments, and passes everything else through. On resume, `true` runs `inner.call`, `false` returns the declined text, and other states go to `inner.resume`. Verify in `tests/tools/test_base.py`:
  - a listed name doesn't reach the inner toolset on `call`;
  - an unlisted name does;
  - unparseable arguments go straight to the inner toolset's error;
  - resume `true` runs it once;
  - resume `false` doesn't run it and gives the declined text;
  - the scenarios of "Confirm list", including that the confirm list never offers a tool.
- [x] 3.3 Update the "Adding a tool group" section of `AGENTS.md`: `Confirmed` wraps `Gated`, and toolsets that ask for input return `ToolOutcome.need` and implement `resume`. Verify by reading it against the code.

## 4. MCP input requests

- [x] 4.1 Pass an `elicitation_callback` that returns `ErrorData` to `Client` in `mcp/client.py` (design D6). Verify in `tests/mcp/test_http.py` against an in-process `MCPServer` whose tool uses `Resolve(...)`/`Elicit`: the first call no longer fails with "did not declare the form elicitation capability".
- [x] 4.2 In `mcp/registry.py`, switch `call_tool` to `client.session.call_tool(..., allow_input_required=True)` and add `classify` for `InputRequiredResult` (design D6):
  - an empty form gives Yes/No;
  - one boolean gives Yes/No/Cancel;
  - one string enum or `oneOf` of at most 8 gives one button each plus Cancel;
  - anything else gives the "can't show" error result.

  Verify in `tests/mcp/test_registry.py` with in-process servers: each showable shape gives the expected options and stored `ElicitResult` answers; a text field, two fields, nine choices, a URL elicitation, a sampling request and two batched questions each give an error result and send nothing back; ordinary calls and results are unchanged.
- [x] 4.3 Add `OpenMcpTools.resume` (design D6). It repeats the call on a fresh connection with `input_responses` and `request_state`, classifies another `InputRequiredResult` with the next round number, gives an error after 10 rounds, and maps `MCPError` `-32602` to the late-answer error. Verify in `tests/mcp/test_toolset.py`:
  - accept and decline reach the tool body (the spike's cases A and C);
  - a second question comes back as a new `InputNeed`;
  - a server rebuilt with a new key gives the late-answer text (case D);
  - changed arguments give the late-answer text (case G);
  - a shared-key server rebuilt between the legs resumes (case E).
- [x] 4.4 Check whether a session-level `call_tool` on a fresh Streamable HTTP connection (no `tools/list` on it) gets `HEADER_MISMATCH` at 2026-07-28, through `server.streamable_http_app()` and `httpx2.ASGITransport` in `tests/mcp/test_http.py`. If it does, re-list the tool and resend once, as the SDK does. Verify with that test for both the first call and the resume. Add the finding to the AGENTS.md MCP notes, together with the input-required notes from design D6 (session-level call, capability declaration, `request_state` replay).

## 5. Parking and resuming a turn

- [x] 5.1 Give `History.recent` optional `conversation_id` and `upto_entry_id` (design D3). Verify in `tests/test_history.py`:
  - entries added after `upto_entry_id` are left out;
  - a conversation archived since (unattended close) is still read by id.
- [x] 5.2 Implement parking in `agent/turn.py` (design D3). Wrap toolsets in `Confirmed(Gated(...), user.confirm)`. When a call returns a need, stop and return `TurnResult.waiting`, with the tail, the call and its index, the counters, the conversation and `upto_entry_id`, images in the tail replaced by the note, and the park recorded in `turn_tool_calls`. Verify in `tests/agent/test_turn.py` with `turn_helpers.py`:
  - a reply with calls [find, add(listed), remind] runs find, parks at add, and doesn't run remind;
  - the tail holds the assistant message and find's result;
  - no reply text is returned.
- [x] 5.3 Implement resume via `TurnInput.resume` (design D3). Rebuild the context from the saved conversation up to `upto_entry_id`, append the tail, run `dispatch_resume` for the waiting call, then the rest of that reply's calls, then the model loop. Carry on the request count and `seq`, with `side_effects` already true. Verify in `tests/agent/test_turn.py`:
  - after **Do it**, add runs, then remind, then the model answers ("Turn goes on after the answer");
  - after **Cancel**, add doesn't run and the model sees the declined text;
  - a second listed call in the same reply parks again ("Two calls that need confirmation");
  - `max_requests_per_turn` counts requests from before the park;
  - an unreachable AI after the resume is a non-retryable failure with the partial notice;
  - an entry recorded while waiting (an `/ask`) is not in the resumed context.

## 6. Request store

- [x] 6.1 Add `alfred/inputs/store.py` `InputRequests` with the D5 transitions:
  - `park` (item `waiting` and the request row in one transaction);
  - `set_message`;
  - `answer` (`open -> answered` and item `-> pending`, filtered on user and expiry, returning `answered`/`not_open`/`not_yours`);
  - `take_answered` (`answered -> running`);
  - `running_for` (crash check);
  - `finish`;
  - `cancel_for(user, reason)` and `expire_due(now)`, both returning the ended requests.

  Verify in `tests/inputs/test_store.py` on Postgres:
  - two concurrent `answer` calls for one request give one `answered` and one `not_open`;
  - another user's `answer` gives `not_yours`;
  - an answer after `expires_at` gives `not_open` and expires the request;
  - `cancel_for` ends only that user's `open` requests and leaves `answered` ones;
  - `expire_due` ends only expired ones;
  - every query filters on `user_name`.
- [x] 6.2 Add the ending path for a request that isn't answered (design D5). It records the fixed entries by kind (chat: an `assistant` entry; ask: `ask_prompt` + `ask_reply`; reminder: `reminder` + `reminder_reply`) with the new `notices.input_not_done`, and marks the item `cancelled`. Verify in `tests/inputs/test_store.py` and `tests/test_notices.py`: each kind records its entries with the reason (new message, expired, question not delivered); a later turn's context shows the chat entry ("Later turn knows").

## 7. Telegram

- [x] 7.1 Add `inputs/render.py` and `TelegramOutbox.send_question` and `edit_question` (design D7):
  - the titles by item kind;
  - escaped `<pre>` detail, cut with a marker to fit 4096;
  - at most 2 buttons per row and `ir:<id>:<index>` callback data;
  - the edit removes the keyboard, and edit failures are logged only.

  Verify in `tests/telegram/test_bot.py` with `fake_bot.py`:
  - the "Confirmation shows the real call", "Request from a service" and "Long arguments" scenarios;
  - a failed edit doesn't raise.
- [x] 7.2 Add `callback_query` to `ALLOWED_UPDATES` and register the tap handler from `inputs/callbacks.py` (design D7). The handler checks the configured user in a private chat, calls `InputRequests.answer`, always answers the callback (with "no longer open" when it had no effect), edits the message on success, and logs strangers by ID only. Verify in `tests/telegram/test_bot.py`:
  - the "Tap acknowledged", "Tap from a stranger", "Someone else taps", "Double tap" and "Old message" scenarios;
  - a tap wakes the worker (`activity.notify`).

## 8. Worker and queue

- [x] 8.1 In `queue.py`, handle `TurnResult.waiting` (design D8): park, send the question and store its id, cancel at once if sending fails, and tell any fallback first. For an item with an `answered` request, take it to `running` and resume without re-recording the user message. An item whose request is already `running` fails with the partial notice. Mark the request `done` when the resumed turn ends. Verify in `tests/test_queue.py` with a scripted agent:
  - "Others go ahead" and "Waiting turn doesn't block";
  - "Answered turn goes first";
  - "Reply after confirmation" with the typing indicator shown only from the tap;
  - "Crash during the confirmed call";
  - "Restart while waiting" (a new `Worker` over the same database);
  - a resumed `/ask` answering `NO_REPLY` sends nothing;
  - a resumed reminder that fails sends the fixed reminder notice.
- [x] 8.2 Make `Queue.enqueue_user` cancel the user's open requests first through a hook wired in `app.py`, for text and attachment messages, then edit their question messages. Verify in `tests/test_queue.py` and `tests/telegram/test_bot.py`:
  - "User moves on": only the new message is answered, the waiting turn gets no reply or failure notice, and the edit says it was cancelled;
  - "Memory command": `/memory` leaves the turn waiting.
- [x] 8.3 Add the expiry loop `inputs/loop.py` on the reminders check interval, started in `app.py` (design D5). Make `History.close_idle` treat `waiting` items as busy. Verify:
  - in `tests/inputs/test_loop.py`: "Nobody answers" (entries recorded, message edited, item `cancelled`);
  - in `tests/test_idle.py`: a user with a waiting item is not closed for inactivity until the request expires.
- [x] 8.4 Whole-flow test in `tests/inputs/test_in_turn.py`:
  - a scripted model on `ana`'s chat turn calls `mcparr_add_media` (listed) and then `alfred_remind_set`, against an in-process MCP server; the fake bot taps **Do it**; the tool runs once, the reminder is stored, and `ana` gets the reply;
  - a second case where the server elicits through `Resolve`/`Elicit` and the tap answers **Yes**;
  - a third where a reminder turn for `diogo` waits and his answer leads to the reminder's message ("Reminder needs confirmation").

## 9. Documentation

- [x] 9.1 Add a README section on confirmations and server questions: the `confirm` list, what the question message shows, buttons, cancel by new message, expiry, all turn kinds, and the server-side `RequestStateSecurity` note (600 s default, restarts). Add `[input_requests]` and `confirm` to the configuration tables, and update the Users and MCP servers sections. Verify the documented config loads (`tests/config/test_example.py`) and every key named exists in `config.py`.

## 10. Integration

- [x] 10.1 Run `make lint` and `make test` and verify both pass.
- [x] 10.2 End-to-end against `ai.lan` (`make run`). Set your own `confirm = ["mcparr_add_media"]`, ask "add Dune (2021)", and check:
  - the question message shows the real arguments;
  - **Do it** adds it once and the reply follows;
  - a second "add Alien" followed by a new message cancels it, and the edit says so;
  - the next turn knows Alien wasn't added.

  If the model retries declined calls or misreports the outcome, stop and show the turn log before changing any texts.

## 11. Fix: other turns acted on a waiting turn's message (0.16.1)

- [x] 11.1 Give `History.recent` a `turn_item_id` and leave out entries of other queue items with a live request (design D3, "Other turns don't see a parked turn"); `Agent._attempt` passes its item. Verify in `tests/test_history.py` and `tests/agent/test_turn.py`: an `/ask` turn run while a chat turn waits doesn't see the waiting message; the resumed turn still does; after the waiting turn ends (answered or cancelled), the next turn sees the message and its reply; entries left out don't count toward `history.max_entries`.
- [x] 11.2 Run `make lint` and `make test`, bump the version to 0.16.1, and add the handover update.

## Workflow follow-up

- Bump the version before building images, then add a handover update (migration `0010`, new optional `confirm` and `[input_requests]` keys).
- Later, separate changes: `RequestStateSecurity` with a shared key and a longer TTL in cookbook and mcparr when they start asking; `alfred_ask_user` on this mechanism.
