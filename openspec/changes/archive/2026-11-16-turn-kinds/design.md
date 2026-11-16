# Design

## Context

See proposal.md for why. This assumes `slice-layout` and
`per-feature-schema` are done: kinds sit in `turns/kinds.py`, payloads in
`turns/payloads.py` (the reminder payload is a snapshot, so building a
reminder turn reads nothing), and turn data in `kernel/turn.py`.

What each kind does today, read from `kinds.py`, `worker.py` and the
input-request store:

```
                 chat                    ask                       reminder
start            idle marker, then       -                         -
                 record user message
                 (+ attachments)
typing           yes                     no                        no
reply            record assistant,       NO_REPLY: nothing         quiet + NO_REPLY: nothing
                 send reply              else record prompt+reply, blank/NO_REPLY: notice text
                                         send "[source] reply"     record reminder+reply, send
failure          finish, then send       finish                    record+send notice, then finish
(non-retryable)  failure notice
                 (not recorded)
unanswered       record assistant        record prompt + notice    record reminder + notice
question         notice                  (not sent; the question message is edited)
question title   "For your request:"     "For a request from X:"   "For reminder #12 (...):"
prompt           -                       ask rules, input line     reminder rules (+ quiet),
                                                                   input line with due/now
```

In every path the item finishes *after* the exchange is recorded and sent
(success, reminder failure), and *before* a notice that isn't part of the
exchange is sent (chat's failure notice). That's the delivery rule the
`finish` callback encodes today without naming it.

## Goals / Non-Goals

**Goals:**

- Everything about a kind is in its own module, and nothing else branches
  on kind.
- Kind decisions are pure and testable without fakes. I/O happens in one
  place.
- Toolsets keep using `isinstance` on the kernel's turn types (the closed
  set chosen in exploration).

**Non-Goals:**

- No new kind, and no plugin mechanism: the set stays closed.
- No change to payload or checkpoint JSON.
- No change to toolsets' rules about which kinds they're offered on.

## Decisions

### D1. The `TurnKind` protocol

```python
class TurnKind[T: TurnInput, P](Protocol):
    tag: ClassVar[str]                  # queue_items.kind
    payload: type[P]                    # its JSON type (to_json / from_json)
    shows_typing: ClassVar[bool]

    def turn(self, item: QueuedItem[P], user: User) -> T
    def opening(self, turn: T) -> Opening
        # entries recorded when the turn first starts, and whether a pending
        # idle marker is told first (chat only)
    def framing(self, turn: T) -> Framing
    def question_title(self, turn: T) -> str
    def answer(self, turn: T, reply: str, now: datetime) -> str | None
        # the text that ends the exchange; None = silent
    def on_failure(self, turn: T, failure: TurnFailed, now: datetime) -> Ending
    def entries(self, turn: T, text: str) -> list[NewEntry]
        # what the exchange records, ending with `text`
    def message(self, turn: T, text: str) -> str
        # what the user is sent for `text`
```

`Ending` is `Silent | Tell(text) | Deliver(text)`. `Tell` is a notice
outside the exchange (sent, not recorded); `Deliver` ends the exchange
(recorded and sent).

Every method is pure, `turn` included, because the reminder payload is a
snapshot (`per-feature-schema` D4). Each kind gets only what its decisions
need: the reminder kind gets the time zone, for the fixed notice.

Alternative considered: keep `start`/`deliver`/`fail` as I/O methods on
each kind. Rejected: the three kinds would each repeat "record, send,
finish" in their own order. That repetition is where the second copy in
`_record_ending` and the unnamed delivery rule came from.

### D2. One module per kind

```
turns/kinds/
  __init__.py   Kinds registry: by tag (decoding an item), by turn type
                (everything after); TurnKind, Opening, Ending, NewEntry
  chat.py       ChatPayload, ChatKind
  ask.py        AskPayload, AskKind
  reminder.py   ReminderPayload, ReminderKind  (input line, quiet rules,
                fixed notice, title "#12 (call mum)")
```

- The payload types move here from `turns/payloads.py`, with the same JSON.
- `Queue.add` gets the tag from the registry by payload type.
- The registry is built once in `app` and passed to the worker, parking,
  intake and the input-request endings.

### D3. The worker and `Exchange`

`turns/exchange.py` holds the only I/O for kinds:

```
Exchange.record(item, turn, entries)      -> conversations
Exchange.send(user, text)                 -> outbox; logged, never raised
Exchange.deliver(item, kind, turn, text)  = record(kind.entries(turn, text))
                                            then send(kind.message(turn, text))
```

The worker:

```
start      o = kind.opening(turn); [tell idle marker]; record(o.entries)
success    text = kind.answer(turn, reply, now)
           if text: deliver(...)                 # exchange first...
           finish("done")                        # ...then the item
failure    retryable -> retry_later (unchanged)
           match kind.on_failure(turn, failure, now):
             Deliver(text): deliver(...); finish("failed")
             Tell(text):    finish("failed"); send(text)
             Silent:        finish("failed")
```

The delivery rule now has a name and one place: the exchange always
completes before the item finishes, so a crash repeats it rather than
losing it. A notice outside the exchange comes after, so a crash loses it
rather than repeating it. The orders match today's for every row of the
Context table.

### D4. Framing replaces prompt rendering on the data

- `agent/framing.py`: the `Framing` protocol has `rules` (which turn rules
  the system prompt adds: ask, reminder, reminder-quiet) and
  `input_line(now, times) -> str | None`.
- `Agent.run(turn, framing)`. The worker passes `kind.framing(turn)`. A
  resumed turn is framed the same way, since parking rebuilds the turn and
  the worker frames it.
- `kernel/turn.py` loses `prompt_flags`, `input_line`, `_PromptFlags` and
  `_TimeFormat`. The turn data classes are plain frozen dataclasses.
- The agent ranks below `turns`, so `Framing` is defined in `agent` and
  implemented in `turns/kinds/`: dependency inversion with no upward
  import.

### D5. Unanswered questions

- `InputRequests` (the store) only changes request rows. Cancelling and
  expiring return the ended rows, and it stops importing the conversation
  store and notices.
- `turns/inputs/endings.py` records each ended request through its kind:
  `exchange.record(item, turn, kind.entries(turn, input_not_done(...)))`.
  It isn't sent; the expiry loop edits the question message as today. The
  cancel-on-new-message path and the expiry loop both call it.
- Parking's title comes from `kind.question_title(turn)`.
  `turns/inputs/titles.py` and the `isinstance` chain go.

### D6. What still says `isinstance`

Toolsets check kernel turn types to decide what to offer:

- `chat_only`;
- reminders offer list-only on reminder turns;
- web seeds `/ask` text.

These are questions about a turn's data, not kind behaviour, and the
closed set keeps them type-checked. After this change, no module outside
`turns/kinds/` and toolset `offered`/`open` methods compares or switches
on kind.

## Risks / Trade-offs

- [Pure decisions make the worker's sequencing the single point that can
  break delivery order] → worker tests keep their assertions on what is
  recorded and sent, and add one per `Ending` that checks finish order
  against record and send with a recording fake.
- [Generic `TurnKind[T, P]` is awkward when looked up by tag (`Any` at the
  registry boundary)] → the registry is the only place that holds kinds
  untyped; each kind is typed within its module and tests.
- [The idle marker in `Opening` is a chat-only flag on a shared type] → it
  describes something a kind can ask for, not a kind check; the other two
  kinds leave it false.

## Migration Plan

Nothing to migrate. Payload and checkpoint JSON are unchanged, so items
queued or parked under 0.17.0 run as before. Rolling back is pinning the
previous image.
