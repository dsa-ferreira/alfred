# Tasks

## 1. Typing indicator

- [x] 1.1 Add `TelegramOutbox.typing()` in `src/alfred/telegram/bot.py`. It returns an async context manager that starts a task sending `send_chat_action(chat_id, ChatAction.TYPING)` every `TYPING_INTERVAL_S` (4 s), and on exit cancels the task and awaits it (design D2). Verify with a test in `tests/telegram/test_bot.py` using `make_bot()`: inside the block, at least one `SendChatAction` request is recorded for the owner's chat with action `typing`, and none is recorded after exit. Make the interval patchable so the test can use a short one.
- [x] 1.2 Catch and log failures inside the loop: the first failure per block at WARNING, later ones at DEBUG, and keep going without catching `CancelledError` (design D3). Verify with tests where `session.on_request` raises a `TelegramBadRequest` for the first `SendChatAction` and then succeeds. The loop keeps sending, one WARNING is logged, and exiting the block raises nothing. Also verify that an exception raised inside the block propagates unchanged.

## 2. Worker

- [x] 2.1 Add the optional `typing` keyword argument to `Worker` in `src/alfred/queue.py` (default: no indicator), and wrap only the `agent.run` call in it for `owner` items (design D1). Verify that `make test` passes with the existing `FakeOutbox` and sweeper fakes unchanged.
- [x] 2.2 Add worker tests in `tests/test_queue.py` with a recording fake indicator that notes enter and exit, and whether a turn is running. Cover these cases: an owner turn enters once and exits before the reply is sent; an `/ask` turn never enters; a retryable failure exits, the backoff wait happens outside the indicator, and the retried turn enters again; a non-retryable failure exits before the failure notice is sent.

## 3. Wiring

- [x] 3.1 Pass `typing=outbox.typing` to `Worker` in `src/alfred/app.py`. Verify with `make test` (including `tests/test_serve.py`) and by running `make run` locally: sending a message that triggers a tool call shows "typing…" in Telegram until the reply arrives.
- [x] 3.2 Run `make lint` and `make test`, and verify both pass.

## Workflow follow-up

- Archive the change after it has been reviewed.
- If this goes into a release, add a handover update for it (no configuration or infrastructure changes).
