# Design

## Context

`ModelClient` (`agent/model.py`) fixes its model name at construction.
`app.serve` builds one client for `[ai] model` and reuses it for summaries
when the summary model is the same tag. The worker runs one queue item at a
time, so there is never more than one turn in flight. alfred's own tools
(`alfred_end_conversation`, the memory tools) are defined and handled in
`agent/turn.py` and are owner-turn only. Fixed notices live in `notices.py`.
Model failures are already split into `ModelUnreachable` (retryable, via the
queue) and `ModelFailed` (not retryable).

## Goals / Non-Goals

**Goals:**
- A small, testable holder of "the current brain" shared by the agent.
- No change to the summary path's behaviour or to the queue's retry logic.

**Non-Goals:**
- Persisting the choice, per-brain settings (timeouts, base URLs, personas),
  or discovering models from the server (`/v1/models`).
- Switching brains from a service (`/ask`) or the HTTP API.

## Decisions

### D1. A `Brains` object holds the list and the current choice

New module `agent/brains.py`: `Brains(default_tag, models: Mapping[alias,
tag])` builds the ordered list of `(name, tag)` (the configured aliases, plus
the default tag under its own name when no alias names it), and keeps
`current` (a name). Methods: `tag` (current tag), `set(name) -> outcome`,
`fall_back() -> bool` (false when already on the default), `is_default`,
`names`. Built once in `app.serve` and passed to `Agent`.

Alternative: a mutable field on `Config`. Rejected: `Config` is frozen and
startup-only by design.

### D2. The model name is chosen per request

`ModelClient.complete(messages, tools, *, model: str | None = None)`: `None`
keeps the constructor's model. The Agent passes `brains.tag` on every
request; `Memory` keeps calling without `model`, so summaries stay on the
summary model (spec: Summaries ignore the chosen brain). The single shared
client in `app.serve` stays as it is.

Alternative: one `ModelClient` per brain. Rejected: more HTTP clients to
close, and nothing per-brain differs except the name.

### D3. The switching tool: `alfred_set_model`

Defined in `agent/turn.py` beside the other local tools, built per turn
because its `enum` comes from `brains.names`. Offered only on owner turns
with two or more brains. Description: switch the model — which the owner may
call the brain, mind, etc. — that answers from the next message on; call it
only when the owner asks. The result text says the switch applies from the
next message (or that it was already current, or lists the valid names). An
invalid name is an error result, not an exception, matching
"Tool errors go to the model". Recorded in the turn transcript like any
other tool call and, like them, marks the turn as having side effects.

The switch is applied immediately to `Brains`, but the running turn keeps
the tag it read at its start (D5), so the closing line comes from the old
brain.

### D4. Brain line in the system prompt

`system_prompt(..., brain=BrainLine(current, tag, others))` appends, after
today's date, one line such as: `You are running on the brain "bonsai"
(model bonsai:8b). Other brains: gemma.` With one brain: `You are running on
the model gemma4:26b-a4b-it-qat.` Kept in the operating-rules block so a
custom persona can't drop it. It changes only when the brain changes, which
already invalidates the AI server's cached prefix.

### D5. Fallback in `Agent.run`

`run` reads `tag = brains.tag` once per attempt and passes it to every
request in that attempt. On `ModelFailed` from the client:

```
if this attempt's brain is the default or already fell back: fail as today
brains.fall_back(); log WARNING (failed brain, default, error)
# (the attempt's brain, not brains.current: a switch earlier in this
#  turn must not decide whether the failed request falls back)
fallback = notices.fell_back(address, failed, default)
if no tool ran: run the attempt again from the start (new system prompt,
                same history), carrying `fallback`
else:           raise TurnFailed(failed notice with partial),
                carrying `fallback`
```

The empty-reply and too-many-steps failures are not model errors and don't
fall back. `ModelUnreachable` is untouched.

`TurnResult` and `TurnFailed` gain `fallback_notice: str | None`. The worker
sends it as its own message, before the reply or failure notice, for owner
and `/ask` turns alike (an `/ask` that ends in `NO_REPLY` or fails still
sends it, because the owner's choice was changed). The notice is not stored
in history: the next system prompt already states the current brain.

Alternative: prefix the notice to the reply text. Rejected: it would break
`NO_REPLY` detection and be stored in history as something the model said.

### D6. Configuration

`AIConfig.models: dict[Name, NonEmptyStr] = {}` (TOML `[ai.models]`).
Duplicate tags are checked in a model validator (error names both aliases);
alias format reuses the existing `Name` type, so a bad key is reported as
`ai.models.<key>`. `Config` exposes nothing new; `app.serve` builds `Brains`
from `config.ai`. The startup log line lists the brains and the current one.

## Risks / Trade-offs

- [A model that answers but never calls tools can't switch back] → It can
  still be asked; if it won't, a restart returns to the startup brain. The
  common case (Ollama 400 "does not support tools") is a `ModelFailed` and
  falls back.
- [Switching to a large model makes the next turn slow while it loads] →
  The existing request timeout already covers cold loads; nothing new.
- [Running a turn again costs one more model request] → Only when the first
  attempt failed before any tool ran; at most once per turn.
- [A restart silently undoes a switch] → Accepted; it's the escape hatch.
  The startup log names the brain.

## Migration Plan

No migration. `[ai.models]` is optional; an existing `alfred.toml` keeps
working with a single brain and no tool. Deploy as a normal release.
