# Proposal

## Why

alfred talks to one model, fixed in `alfred.toml`; trying another model means
editing the file and restarting. The owner wants to switch models ("brains")
from the chat — "change brains to gemma", "set your brain to bonsai" — pick
from a short list they configured, and ask which brain is in use.

## What Changes

- `[ai.models]` in `alfred.toml` lists the brains as `alias = "model tag"`
  pairs. `[ai] model` stays the startup brain; when its tag is not in the list
  it is still a brain, under its tag. Aliases are lowercase names; two aliases
  for the same tag are a startup error.
- A new owner-only tool lets the model switch brains when the owner asks, in
  any words ("model", "brain", ...). It accepts only the configured aliases.
  The switch takes effect from the next turn. It is offered only when there are
  at least two brains.
- The choice is kept in memory: a restart goes back to `[ai] model`.
- Every turn (owner and `/ask`) uses the current brain, and its system prompt
  names the current brain and the others, so "which brain are you using?" gets
  a correct answer.
- Conversation summaries keep using `[memory] summary_model`, else
  `[ai] model`, never the chosen brain.
- Fallback: when a model request on a brain other than the startup one fails
  with a non-retryable model error (for example the model is unknown or can't
  use tools), alfred switches back to the startup brain and tells the owner
  with a fixed notice. If no tool had run in that turn, the turn runs again on
  the startup brain; otherwise it fails as today. An unreachable AI server does
  not trigger the fallback.
- Switches and fallbacks are logged.

## Capabilities

### New Capabilities

- `model-selection`: the configured brains, switching between them from the
  chat, telling the model which brain is current, and falling back to the
  startup brain when the chosen one fails.

### Modified Capabilities

- `agent-loop`: turns are sent with the current brain rather than always the
  configured model.
- `persona`: a new fixed notice for falling back to the startup brain.
- `service-configuration`: `[ai.models]` is a configurable setting, with its
  validation rules.

## Impact

- `src/alfred/config.py`: `[ai.models]` and its validation.
- `src/alfred/agent/model.py`: the model name is chosen per request.
- `src/alfred/agent/`: the current-brain state, the switching tool, the
  fallback in the turn, and the brain line in `prompts.py`.
- `src/alfred/notices.py`, `src/alfred/queue.py` (sending the fallback
  notice), `src/alfred/app.py` (wiring, startup log line).
- `alfred.toml.example`, `README.md`, tests for config, prompts, turn, queue
  and notices.
- No database, API or dependency changes.
