# Proposal

## Why

`slice-layout` gave every package a rank, but the ranks exist only in the
import-linter contract and AGENTS.md. On disk, all packages sit side by
side under `src/alfred/`: `facts/` next to `kernel/`, `turns/`, `db/` and
`telegram/`. So the tree, and every import line, hides the layer a module
belongs to.

`slice-layout` turned down ranks as directories because of the longer
import paths. With the layout now settled, seeing the rank in every path
is worth that extra segment.

## What Changes

A pure move. No package's contents change: `facts/` keeps exactly its
files, just under `features/`.

- **One package per rank** under `src/alfred/`:
  - `adapters/` (`telegram`, `http`)
  - `runtime/` (`turns`, `agent`)
  - `features/` (`archive`, `facts`, `reminders`, `web`, `mcp`, `toolbox`,
    `conversations`, `attachments`, `brains`)
  - `infra/` (`db`, `llm`, and the loose modules `backoff.py`, `times.py`,
    `settings.py`)
- **Not moved:** the entry modules (`app`, `cli`, `config`, `schema`,
  `startup`) stay at the root, and `kernel/` stays where it is, since it's
  the only package in its rank.
- **Imports** of moved modules are rewritten in src, tests, migrations and
  scripts.
- **Tests** mirror the new tree (`tests/features/facts/...`).
- **The import-linter contract** splits in two: one for the ranks, plus
  one per rank for the order inside it.
- **AGENTS.md's "Packages and ranks"** describes the tree, and where a new
  package goes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This is a pure refactor; the change sets `skip_specs: true`.

## Impact

- Code: every package except `kernel/` moves; the root keeps only the entry
  modules. `pyproject.toml`'s `alfred.cli:run` entry point doesn't change.
- Logs: logger names follow the module paths, so log lines show
  `alfred.runtime.turns.worker:` instead of `alfred.turns.worker:`. No
  handover verify step or README line depends on a logger name.
- Tooling: `pyproject.toml` import-linter contracts; `.git-blame-ignore-revs`.
- Docs: AGENTS.md and README paths.
- Release: no schema, config or stored-data change, so no handover update.
