# Design

## Context

See proposal.md for why. Today `src/alfred/` holds 18 packages and 8
modules at one level. The ranks are declared in a single import-linter
`layers` contract (`containers = ["alfred"]`, `exhaustive = true`) that
lists every top-level package, and they're copied in AGENTS.md. About 900
import lines in src, tests, migrations and scripts name an `alfred.<package>`
path. The tests already mirror the packages (`tests/facts/`,
`tests/turns/`, ...).

## Goals / Non-Goals

**Goals:**

- The directory tree and every import path show a module's rank.
- The contract follows the tree: ranks at the top, order within a rank
  inside it.

**Non-Goals:**

- No change to any module's contents beyond its import lines.
- No change to the rank table, the feature order or the layout rules.

## Decisions

### D1. The tree

```
src/alfred/
  app.py cli.py config.py schema.py startup.py      entry (root)
  adapters/   telegram/  http/
  runtime/    turns/  agent/
  features/   archive/ facts/ reminders/ web/ mcp/ toolbox/
              conversations/ attachments/ brains/
  kernel/                                            (unchanged)
  infra/      db/  llm/  backoff.py  times.py  settings.py
```

- **Entry stays at the root.** Nothing imports those five modules except
  each other and `migrations/env.py`, which imports `alfred.schema`. They
  are the top of the stack, and `alfred.cli:run` keeps its path.
- **`kernel/` stays.** It's a rank with one package, and `kernel/kernel/`
  would add a level that says nothing.
- Each rank package has an `__init__.py` with a one-line docstring naming
  the rank, and nothing else.

Alternative considered: `alfred/entry/` for the entry modules. Rejected:
it would move the entry point and add a level for five modules that
nothing below imports.

### D2. Contracts

```toml
[[tool.importlinter.contracts]]
name = "ranks"
type = "layers"
containers = ["alfred"]
exhaustive = true
layers = [
    "app : cli : config : schema : startup",
    "adapters",
    "runtime",
    "features",
    "kernel",
    "infra",
]

# order within each rank, one contract per rank package
name = "adapters": layers ["telegram | http"],                 containers ["alfred.adapters"]
name = "runtime":  layers ["turns", "agent"],                  containers ["alfred.runtime"]
name = "features": layers ["archive | facts | reminders | web | mcp | toolbox",
                           "conversations", "attachments", "brains"],
                                                               containers ["alfred.features"]
name = "infra":    layers ["db | llm | backoff | times", "settings"],
                                                               containers ["alfred.infra"]
```

The per-rank contracts are also `exhaustive = true`, so a new package
under a rank has to be placed in its order. `exclude_type_checking_imports`
stays `false`.

### D3. The move

- Rewrite with `git mv` plus a scripted rewrite of `alfred.<pkg>` to
  `alfred.<rank>.<pkg>`, in both import forms (`from alfred.x import` and
  `import alfred.x`). Also rewrite string references:
  - `monkeypatch.setattr("alfred.x....")` targets in tests;
  - logger names in `caplog` assertions;
  - paths in comments and docs.

  The script is run once and not committed.
- Tests move with their packages: `tests/facts/` becomes
  `tests/features/facts/`, `tests/db/` becomes `tests/infra/db/`, and
  `tests/test_times.py` becomes `tests/infra/test_times.py`. Shared test
  helpers (`conftest.py`, `fakes.py`, `helpers.py`) stay at `tests/`.
- `scripts/check_layout.py` resolves imports by module name, so nesting
  shouldn't affect it. Group 1 verifies that.
- One move-only commit, added to `.git-blame-ignore-revs`. The docs and
  contract follow in their own commit.

## Risks / Trade-offs

- [String references missed by the rewrite (mock targets, `caplog`
  logger names) fail at runtime, not import time] → `make test` catches
  mock targets. A grep for `"alfred\.(archive|facts|...)` in src and tests
  must come back empty.
- [Longer import lines push some imports past the line length] →
  `make format` rewraps them, in the same move commit.
- [Anything grepping logs by logger name sees new names] → nothing in the
  repo does. The release notes for the version that ships this mention it.

## Migration Plan

Nothing to migrate. Rolling back is pinning the previous image.
