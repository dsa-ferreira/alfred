# Design

## Context

See proposal.md for why. The current state that shapes the approach:

- 71 modules, about 13k lines, with about 20k lines of tests. The tests are
  the safety net: no phase changes what a test asserts, only how it imports
  and builds things.
- Measured with a quick AST and regex pass (approximate): 48 of 71 modules
  define something used elsewhere below something used only in the module;
  about 109 top-level names without `_` are used only in their own module,
  and about 22 names with `_` are imported elsewhere.
- Package pairs that import each other today (ignoring `tools.base`, the
  shared contract):

  ```
  attachments <-> queue     intake -> queue.Queue;  queue -> attachments.NewAttachment
  attachments <-> history   history -> handles;     inline -> HistoryEntry, tools -> current_id
  history     <-> memory    history -> transcript.recall_label;  memory.search -> current_id
  agent       <-> attachments   turn -> attachments.inline;  attachments.tools -> agent.brains
  ```

- A parked turn's progress is stored as JSON (`Waiting.to_json()`), and
  rows written by 0.16.0 (no `entry_ids`) and 0.16.1 may be waiting when
  this ships.
- No other change is in flight, so the large mechanical diffs conflict with
  nothing.

## Goals / Non-Goals

**Goals:**

- A reader can tell from a name and its position whether other modules
  use it, and the rule is checked, not remembered.
- Each module has one job; packages form a graph without cycles.
- Each phase leaves `make lint` and `make test` green and is its own commit
  (or small series), so the work can stop after any phase.

**Non-Goals:**

- No behaviour change, no new features, no performance work.
- No database migration and no change to the stored JSON of parked turns.
- No change to `AGENTS.md`'s toolset contract beyond following renames.
- Not adding a type checker.

## Decisions

### D1. The layout rule

```
module
  imports (TYPE_CHECKING block included)
  constants and variables      log, type aliases, TypeVars, public and _private constants
  public supporting types      results, exceptions, protocols, small dataclasses
  public main items            the module's main class(es) or function(s)
  _internal items              classes and functions used only in this module

class
  class attributes (dataclass fields included)
  __init__ and other dunders
  public methods
  _private methods
```

- **Public** means used by another module: anything under `src/alfred/`,
  anything under `migrations/`, or an entry point in `pyproject.toml`
  (`alfred.cli:run`). Tests don't count. A name is public exactly when it
  has no leading `_`.
- Tests may import `_` names where they need to.
- **Python's definition order wins.** A `_` item that a later item needs at
  import time (a base class, a decorator, a default value, a
  `default_factory`, something called at module level) goes directly above
  its first such user, not in the internal section.
- A method is public when something outside its class calls it, including
  through a protocol (`Toolset.open`, `call`, `resume`). Method names follow
  the same `_` rule.

Alternative considered: main item first and supporting types after it
(top-down reading). Rejected by preference: supporting types first.

### D2. The layout check

A stdlib-only script, `scripts/check_layout.py`, run by `make lint` after
ruff. It parses `src/alfred/` and `migrations/` with `ast` and fails with
`path:line: message` on:

1. a top-level name without `_` that no other source module imports or
   references, unless it is an entry point;
2. a top-level `_` name imported by another source module;
3. at module level: a constant or variable after a class or function, or a
   public class or function after a `_` one, except under D1's
   definition-order exception (the `_` item is used at import time by the
   item right after it);
4. in a class: a class attribute after a method, a dunder after a non-dunder
   method, or a public method after a `_` one;
5. two packages that import each other (the graph from Context), so phase
   3's result can't regress.

It does not check whether a method is used outside its class (calls through
protocols can't be resolved statically) or which public item is "main"
versus "supporting". Review covers both.

Alternative considered: a pytest test. Rejected: you asked for it in lint,
and it is about style, not behaviour. A ruff plugin isn't possible (ruff
has no plugin API).

### D3. Mechanical commits

Phases 1 and 2 are commits that only move, rename or rewrite comments. Each
such commit's hash goes into `.git-blame-ignore-revs` (and `git config
blame.ignoreRevsFile` is documented in `AGENTS.md`), so `git blame` skips
them. Reorder commits are reviewed with `git diff --color-moved`.

Within phase 1, renames and moves are separate commits, so each diff shows
one kind of change.

### D4. Comment policy

- Remove `(change X, design DY)`, `(design DY)` and `(DY)` references.
- Keep the reason the comment gives. Delete a comment that only says where
  the code came from.
- Module docstrings describe the module as it is now, in present tense.
- Notes about stored data from older versions (for example "turns parked by
  0.16.0 saved no ids") stay: they explain why code exists today.

### D5. Target dependency graph

```
                    app, cli
                       |
          +------------+-------------+
          v            v             v
       telegram       api          worker ------+
          |            |             |          |
          +------+-----+      +------+------+   |
                 v            v             v   v
               queue        agent ------> inputs, reminders, memory,
                 |            |            attachments, web, mcp, tools/*
                 v            v                  |
              history <-------+------------------+
                 |
                 v
     db, config, brains, tools.base, attachments.handles
```

The moves that get there:

- **`history.py` is storage only.** `to_messages`, `user_text`, `stamped`,
  `strip_stamp`, `format_stamp` and `Inline` move to `agent/messages.py`,
  with the imports of `memory.transcript` and `reminders.when` they need.
  `history` keeps `refs_by_entry` from `attachments.handles`, a plain query.
- **`InlineAttachments` and `ImageCache` move to `agent/`**: they turn
  history entries into model content, which is building a turn.
  `load_image`, used by `attachments.tools`, stays in `attachments`.
- **`agent/brains.py` becomes `alfred/brains.py`**: it is shared state
  (the current brain) used by `attachments.tools`, `tools.brains`, the
  agent and `app`, and imports only `config`.
- **`attachments.intake` takes an `Enqueue` protocol** with the one method
  it calls, instead of importing `Queue`.
- **`queue.py` splits**: `queue.py` keeps `Queue` and `QueueActivity`
  (storage and the signal); `worker.py` gets `Worker` and the protocols it
  needs. `api` and `telegram` then don't depend on the agent through
  `queue`.

Exact module names may change during implementation if a better one shows
up; what is fixed is that no package pair imports each other (D2 check 5).

### D6. Required dependencies

Collaborators (`InputRequests`, outboxes, reminder lookup, facts, inline
attachments, typing indicator) become required constructor arguments of
`Worker` and `Agent`. Values with a natural default stay optional with
that default: clocks (`now`), the time zone, the expiry time.

Tests get small fakes in one place (`tests/fakes.py`, next to the existing
`tests/agent/turn_helpers.py`). Branches that only run when a collaborator
is missing, such as "not wired (only in tests)", are deleted.

Alternative considered: keep `None` and add null objects in `app.py`.
Rejected: the branches would stay, just elsewhere.

### D7. Parked turns

- **A typed `Checkpoint`** (frozen dataclass, beside `Waiting` in the agent)
  holds call, call index, remaining calls, tail, request and call counters,
  conversation id, newest entry id, reset flag and entry ids.
  `to_json()` writes exactly the keys written today, and `from_json()` reads
  both the 0.16.0 shape (no `entry_ids`) and the current one. So no data
  migration is needed, and an image rollback to 0.16.1 still resumes turns
  parked by the new code. A test pins both shapes.
- **`History.recent()` becomes two methods**: one for the current window
  (leaving out other parked turns' entries) and one for exactly the window a
  checkpoint names. The four optional filters go away.
- **Parking and resuming move out of `Worker`** into one class in
  `alfred/inputs/` that does three things:
  - checks whether alfred stopped during an answered call;
  - takes the answered request and builds the resumed turn;
  - stores the request and sends the question.

  `Worker._process` calls it at those three points.

### D8. The turn attempt

`Agent._attempt` splits into `_build_context()` and `_loop()`.
`_build_context()` reads the history, builds the system prompt, inlines
attachments, adds the input for the turn's kind, resets per-attempt toolset
state, and returns the messages with where they came from (for the
checkpoint). `_loop()` runs the model and tool loop. Resuming is one branch
between the two.

### D9. Turn kinds

- **Data stays in `tools.base`**, which imports nothing else from alfred.
  `ChatTurn`, `AskTurn` and `ReminderTurn` are frozen dataclasses with the
  shared fields (`user`, `text`, `queue_item_id`, `resume`) and only their
  own extras. `TurnInput` becomes their union. Toolsets that check the
  kind use `isinstance` (`chat_only` becomes
  `isinstance(turn, ChatTurn)`).
- **Behaviour that needs no dependencies goes on the data classes.** That
  means the input message the agent adds (ask prompt, reminder line) and
  the system prompt flags.
- **Behaviour that needs the history or outbox goes in per-kind handlers
  next to the worker**: delivering and recording the reply, what a failure
  sends, and whether the typing indicator shows. `Worker._complete` and
  `_turn_failed` dispatch to the handler instead of branching.
- **The queue keeps storing `"user"`**: no migration. The code calls the
  kind "chat" everywhere, and the stored value is mapped in one place in
  `queue.py`.

Alternative considered: putting the behaviour on the data classes too.
Rejected: `tools.base` must not import alfred's history or outbox.

### D10. Startup

`app.serve()` splits into:

- `build_services(config, stack)`: builds everything, registering each
  resource's close on an `AsyncExitStack`, so shutdown order follows build
  order;
- `run_tasks(...)`: starts the tasks, waits, and stops them;
- the startup log line in its own function.

The failure handling stays: stop on the first task that stops, and redact
the bot token.

## Risks / Trade-offs

- [Large mechanical diffs hide a real change] → Phases 1 and 2 only move,
  rename or edit comments; they are reviewed with `--color-moved` and listed
  in `.git-blame-ignore-revs`.
- [A refactor changes behaviour quietly] → no phase changes what a test
  asserts; where a phase moves logic (5, 7), its tests are moved, not
  rewritten.
- [Parked turns written before the deploy can't be resumed] → `Checkpoint`
  keeps the stored JSON identical and reads the 0.16.0 shape; a test covers
  both.
- [The layout check flags false positives] → it reports `path:line`, and
  the few real exceptions (entry points) live in one constant in the script;
  no inline suppression comments.
- [The rule "supporting types first" is subjective] → the check enforces
  the parts that are mechanical; reviews cover the rest.
- [Eight phases is long] → each phase is independent and shippable; the
  change can be archived after any phase by trimming the remaining tasks.

## Migration Plan

Nothing to migrate. Deploy as usual; rolling back to the previous image is
safe because the database schema and the stored parked-turn JSON don't
change.
