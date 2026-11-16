# Proposal

## Why

The code is hard to follow. Each change was added cleanly but in place, so
modules now read as a history of features rather than a model of how alfred
works: comments point into archived changes, turn kinds are `if` branches in
several files, parking a turn runs through every layer, and several packages
import each other. Behaviour is right and well tested; the shape is not.

## What Changes

A refactor, done in eight phases. Nothing observable changes: no config,
API, database schema, Telegram behaviour or stored data format changes, and
turns parked by 0.16.x still resume.

1. **Layout rule.** A written rule for the order of things in a module and a
   class, and for which names get a leading `_`, enforced by a check that runs
   in `make lint`. Every module is reordered and renamed to follow it.
2. **Comments.** The 238 `(change X, design DY)` tags go; each comment keeps
   the reason it gives. Module docstrings describe the code as it is.
3. **History split and import cycles.** `history.py` becomes storage only;
   building the model's messages moves to `alfred.agent`. `queue.py` splits
   into the queue and the worker. No two packages import each other.
4. **Required dependencies.** `Worker` and `Agent` take what they need;
   optional collaborators that exist only for tests are replaced by fakes,
   and the branches only tests take are removed.
5. **Parked turns.** A typed checkpoint replaces the JSON dict a parked turn
   is resumed from; `History.recent()` loses its four optional filters in
   favour of two methods; parking and resuming move out of `Worker`.
6. **The turn attempt.** `Agent._attempt` splits into building the context
   and running the model and tool loop.
7. **Turn kinds.** One class per kind (chat, ask, reminder) owns its input,
   how its reply is delivered and recorded, and how it fails, in place of
   the flat `TurnInput` and the per-kind branches in the worker.
8. **Startup.** `app.serve()` splits into building the services, running
   the tasks, and closing resources.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This is a pure refactor; the change sets `skip_specs: true`.

## Impact

- Code: every module under `src/alfred/` (reordering and renaming), with
  structural changes in `history.py`, `queue.py`, `agent/turn.py`,
  `tools/base.py` (`TurnInput`), `inputs/`, `attachments/`, `app.py`.
- Tests: imports and constructor calls follow the renames and required
  dependencies; new fakes in `tests/`. No test changes what it asserts.
- Tooling: a layout check script, a `make lint` step, and
  `.git-blame-ignore-revs` for the mechanical commits.
- Docs: `AGENTS.md` gains the layout rule; its "Adding a tool group" section
  follows any renamed or moved names.
- Release: no version bump of its own beyond the project's usual practice;
  no handover update is needed (nothing deployable changes).
