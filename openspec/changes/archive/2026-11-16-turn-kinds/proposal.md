# Proposal

## Why

A turn's kind (chat, ask, reminder) is one concept spread over parallel
hierarchies and string switches. After `slice-layout` and
`per-feature-schema`, it still lives in five places:

- the turn data classes in `kernel/turn.py`, which also render the prompt
  (`prompt_flags`, `input_line`);
- the `ChatKind`/`AskKind`/`ReminderKind` handlers in `turns/kinds.py`,
  joined to the data by a stored string and an `assert isinstance`;
- the payload types in `turns/payloads.py`;
- question titles in `turns/inputs/titles.py` and parking's `isinstance`
  chain;
- a second copy of each kind's recording rules in the input-request
  store's `_record_ending`.

Adding a kind means touching all of them. The handlers also mix deciding
with doing (each one records, sends and finishes itself), and the order of
those calls hides a delivery rule that nothing names.

Third of three changes, after `slice-layout` and `per-feature-schema`.

## What Changes

A refactor. Nothing observable changes: the same entries are recorded, the
same messages are sent in the same order, and failures and unanswered
questions end the same way. The stored payload and checkpoint JSON don't
change.

- **One module per kind.** `turns/kinds/chat.py`, `ask.py` and
  `reminder.py` each own the kind's tag, payload type, how it builds its
  turn, its prompt framing, its question title, and its decisions about
  the reply, a failure and what the exchange records.
- **Kinds decide, the worker does.** A `TurnKind` makes pure decisions
  (what text ends the exchange, which entries record it, what message is
  sent, how a failure ends). One `Exchange` helper in `turns` records and
  sends. The rule nothing named today is now written down: the exchange
  is recorded and sent before the item finishes, and a notice outside the
  exchange is sent after.
- **Plain turn data.** `ChatTurn`, `AskTurn` and `ReminderTurn` in the
  kernel lose their prompt rendering. The agent takes a `Framing` from the
  kind (`Agent.run(turn, framing)`), and stays as kind-agnostic as it is
  tool-agnostic.
- **The input-request store stops recording history.** Ending an
  unanswered request returns the ended rows, and the turn side records
  each one through its kind.
- Removed: `turns/payloads.py`, `turns/inputs/titles.py`, the string
  switches on kind, and the `Kind` protocol with its `finish` callback.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This is a pure refactor; the change sets `skip_specs: true`.

## Impact

- Code: `turns/kinds/` (new package), `turns/worker.py`,
  `turns/inputs/` (store, parking, loop), `kernel/turn.py`, `agent/`
  (`turn.py`, a new `framing.py`, `prompts.py`), `app.py` wiring.
- Tests: per-kind decision tests with no fakes. Worker, parking and
  input-request tests keep their assertions; only construction changes.
- Docs: AGENTS.md describes the kinds and how to add one.
- Release: no schema or stored-data change, so no handover update. A
  0.17.0 database runs unchanged.
