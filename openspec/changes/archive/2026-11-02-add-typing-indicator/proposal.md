# Proposal

## Why

After the owner sends a message, the chat shows nothing until the reply
arrives. A turn with several model requests and MCP tool calls can take a
while, and from the chat alone the owner can't tell whether alfred is
working or stuck. Telegram's "typing…" chat action is the usual answer, and
aiogram already supports it.

## What Changes

- While the AI turn for an owner message is running, the owner's chat shows
  "typing…". It is resent for the whole turn, so a long turn keeps showing
  it, and it stops when the turn ends.
- No typing is shown while a message waits in the queue, while it waits for
  a retry (for example, while the AI is unreachable), for `/ask` turns, or
  while a conversation summary is made after a reset.
- A failure to send the typing action is logged and never affects the turn
  or its reply.
- No new configuration.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `telegram-chat`: a new requirement that the owner's chat shows a typing
  indicator while an owner turn runs.

## Impact

- `src/alfred/telegram/bot.py`: a typing indicator that resends the action
  until it is stopped.
- `src/alfred/queue.py`: the worker runs owner turns inside the indicator.
- `src/alfred/app.py`: wires the indicator into the worker.
- Tests for the indicator (`tests/telegram/`) and the worker
  (`tests/test_queue.py`).
- No API, database, configuration or dependency changes.
