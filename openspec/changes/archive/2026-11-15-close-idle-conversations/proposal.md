# Proposal

## Why

The owner often forgets to end a conversation. It then keeps growing and
mixes topics. When they do end it, usually just before starting something
new, the next message waits: the queue worker summarises the archived
conversation before it takes the next item, and Ollama on `ai.lan` runs one
request at a time. A forgotten conversation has a long transcript, so it is
also the slowest kind to summarise.

## What Changes

- Ending a conversation no longer waits for its summary. alfred archives
  it, starts the new one, and sends a marker with the ids only:
  `— #41 archived —` and `— new conversation #42 —`. The marker no longer
  has the title. All summaries are made by the memory sweeper in the
  background.
- New optional idle close: a configurable time after a user's last own
  message, alfred archives that user's current conversation in the
  background and starts a new one. Only the user's own messages count.
  Notices, `/ask` turns and reminders don't keep a conversation open. A
  conversation with no user message is never closed this way, and neither
  is one whose user has a message in the queue. The user is told when
  they next write: the marker
  `— #41 closed after inactivity —` / `— new conversation #42 —` comes
  before the reply. Off when unset.
- Summaries give way to users' turns. The sweeper starts a summary only
  when the queue is empty and has been quiet for a configurable time. A
  queue item that arrives while a summary's model request runs cancels
  that request. The summary request is streamed, because Ollama only
  notices a dropped client on a streamed request. The summary is made
  again once the queue is quiet, and the cancellation doesn't count as a
  failed attempt (no backoff).
  Embedding requests go to the embedding service, not the model server,
  so they are never cancelled.
- New config keys: `[history] idle_close_after_h` (unset: off) and
  `[memory] summary_quiet_s`.
- Out of scope: closing on a topic change, per-user idle times, moving
  notices into the new conversation, a "previous conversation" hint in the
  new conversation's context, a separate model server for summaries.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `conversation-history`: the reset marker loses the title and is sent
  without waiting for the summary; new requirement for closing idle
  conversations and telling the user on their next message.
- `conversation-memory`: every summary is made in the background; summaries
  wait for a quiet queue, and a queue item cancels a summary in progress
  without counting as a failure.
- `service-configuration`: the idle close time and the summary quiet period
  are settable in the file.

## Impact

- `src/alfred/queue.py`: `_reset` stops calling the summariser and wakes
  the sweeper instead; `reset_marker` changes; the worker sends a pending
  idle-close marker at the start of a user's turn; the queue reports when
  items arrive and when it goes quiet.
- `src/alfred/memory/sweeper.py`, `src/alfred/memory/summaries.py`:
  waiting for a quiet queue, and cancelling the summary request.
- `src/alfred/history.py`: idle close, and recording how a conversation
  ended and whether its marker was sent.
- New background task for idle close, started in `app.py`.
- `src/alfred/config.py`, `alfred.toml.example`: the two new keys.
- Migration `0006`: two columns on `conversations`.
- `src/alfred/agent/model.py`: a streamed request for summaries. Chat
  turns are unchanged.
- No API or Telegram command changes, and Ollama is unchanged. Measured
  on `ai.lan` (Ollama 0.40.13, `bonsai-2:27b`): a dropped non-streamed
  request runs to the end; a dropped streamed one stops within about a
  second once tokens are being generated, but not while the prompt is
  still being read (about 9 s for a 60,000-character transcript).
