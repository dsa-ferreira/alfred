# Design

## Context

Owner messages don't go to the AI from the Telegram handler. `on_message`
(`src/alfred/telegram/bot.py`) only queues them. The single `Worker`
(`src/alfred/queue.py`) later takes the head item and calls
`self._agent.run(turn)` in `_process`. A retryable `TurnFailed` puts the item
back with a backoff and `_process` returns. The item is processed again
later.

The worker only knows Telegram through the `Outbox` protocol (`send`), which
`TelegramOutbox` implements. The `/notify` API uses the same outbox. Test
fakes for it exist in `tests/test_queue.py` and `tests/memory/test_sweeper.py`.

Telegram shows a chat action for about 5 seconds, or until the bot sends a
message to that chat. Holding it for longer means resending it.

aiogram 3.31 has `aiogram.utils.chat_action.ChatActionSender`, which resends
an action every 5 seconds from a background task. Its loop doesn't catch
errors from `send_chat_action`. One failed request ends the task, typing
stops for the rest of the turn, and asyncio only reports the error as
"Task exception was never retrieved".

## Goals / Non-Goals

**Goals:**
- Typing visible for the whole running owner turn, including long tool calls.
- The worker stays free of aiogram, and its tests keep their fakes simple.

**Non-Goals:**
- Any acknowledgement while the message is waiting in the queue or for a
  retry, such as a reaction or a "queued" notice.
- Other chat actions, such as upload or record.
- Configuration for the indicator.

## Decisions

### D1: A separate, optional typing hook on the worker

`Worker` gets a keyword argument `typing: Callable[[], AbstractAsyncContextManager[None]] | None = None`.
When it is `None`, nothing is shown. `_process` wraps the `agent.run` call in
`async with self._typing():` for `kind == "owner"` items only:

```
_process(item)
  owner: history.append(owner)
  async with typing()        <- owner items; nullcontext otherwise
      result = agent.run(turn)
  except TurnFailed ...      <- indicator already stopped
  _complete / _turn_failed   <- reply sent after typing ends
```

The indicator covers only `agent.run`. The history write before it is fast.
Everything after it (sending the reply, the reset and its summary) runs with
the indicator already stopped. A retried turn enters the block again on its
next attempt, so typing comes back exactly when the turn runs again. The
backoff sleep is in `step`, outside `_process`, so it never shows typing.

Alternative: add `typing()` to the `Outbox` protocol. Typing is about the
worker's turns, not about sending messages. `/notify` would carry a method it
never uses, and every outbox fake would need one. A separate optional hook
leaves the fakes and `/notify` unchanged.

### D2: Our own resend loop instead of `ChatActionSender`

`TelegramOutbox` gets a `typing()` method that returns an async context
manager. On enter, it starts a task that loops: send
`bot.send_chat_action(chat_id, ChatAction.TYPING)`, then sleep for
`TYPING_INTERVAL_S` (4 seconds). On exit, it cancels the task and awaits it.
`app.py` passes `typing=outbox.typing` to `Worker`.

The interval is 4 seconds, not 5, so a slow request doesn't leave a gap
before the next one. It is a module constant, not a config value, because it
follows Telegram's display time and not the owner's preference.

Alternative: `ChatActionSender.typing(...)`. It does the same thing in fewer
lines, but a single Telegram error stops typing for the rest of the turn (see
Context). That's the case the spec's "Telegram recovers mid-turn" scenario
rules out. The loop is about 20 lines.

### D3: Errors are logged and swallowed inside the loop

Each `send_chat_action` call is in `try/except Exception`. The first failure
in a turn is logged at WARNING with the exception's type and message. Later
failures in the same turn are logged at DEBUG, so an outage during a long
turn doesn't add a warning every 4 seconds. The loop keeps going.
`CancelledError` is not caught, so exit still stops the loop.

Exiting the context manager never raises from the indicator. If the turn
raises, that exception propagates unchanged, so `_process` handles
`TurnFailed` and unexpected errors exactly as it does today.

## Risks / Trade-offs

- [The typing indicator outlives the turn by up to 5 seconds when the turn
  ends without a message (an owner turn always sends a reply or a notice, so
  this is rare)] → Accept it. Telegram has no "stop typing" call, and sending
  the reply clears the indicator at once.
- [Telegram rate limits during long turns] → One request every 4 seconds to
  a single chat is far below the limits. A 429 is a failed request under D3
  and doesn't affect the turn.
- [The cancelled task is left running if exit is skipped] → `async with`
  always runs exit, including on cancellation at shutdown. Exit awaits the
  task, so no task is left pending.

## Migration Plan

None. Deploying the new image turns it on. Rolling back removes it.
