# Proposal

## Why

`untangle-codebase` fixed the inside of each module, but not the overall
shape. Where a module lives doesn't tell you its layer: `src/alfred/` holds
14 modules at mixed altitudes (`history`, `queue` and `worker` next to
`config` and `backoff`). Some features are spread across packages
(conversations live in six places), and some modules hold several jobs
(`tools/base.py` is the turn domain, the toolset contract, access policy and
user questions at once). Worse, the lowest layer depends on the top:
`config` imports every feature through `tools/catalog.py`, behind a lazy
import that hides the cycle.

This is the first of three changes (then `per-feature-schema`, then
`turn-kinds`). It sets the package layout the other two build on.

## What Changes

A refactor. Nothing observable changes: no config key, API, Telegram
behaviour, database schema or stored data changes.

- **Ranked layers.** Every package belongs to one rank, and a package
  imports only from its own rank or lower. The ranks are entry (`app`,
  `cli`, `config`), adapters (`telegram`, `http`), runtime (`turns`,
  `agent`), features, kernel and infra.
- **Feature slices.** Each feature is one package that owns its domain
  types, persistence, model-facing tools, background jobs, config section
  and user-facing text. Features may depend only on the features ranked
  below them, in a declared order.
- **A kernel** replaces `tools/base.py`, split by job: turns, the toolset
  contract, access policy (allow and confirm lists), questions to the user,
  model content and users.
- **Config inverted.** Each feature defines its own section; the composed
  `Config` and its loader sit at the entry rank, and nothing below imports
  them. The startup check that exact `alfred_*` allow-list entries name a
  real tool moves to `app` and asks the toolsets for their names.
  `tools/catalog.py` is deleted.
- **New packages.** `conversations/` (from `history.py`,
  `db/conversations.py`, `idle.py`, the end-conversation tool and the
  transcript). `archive/` and `facts/` (from `memory/`). `turns/` (queue,
  worker, kinds, parked turns). `http/` (from `api.py`). `llm/` (chat and
  embedding clients). `toolbox/` (calculator, dates).
- **Single-job modules.** `memory/facts.py`, `memory/search.py`,
  `agent/turn.py`, `reminders/when.py` and `config.py` are split. The
  duplicated preemptible model request becomes one.
- **Notices per feature.** `notices.py` is split into each feature's own
  `notices.py`.
- **Enforced.** `import-linter` contracts in `make lint` check the ranks and
  the feature order. They replace check 5 (package cycles) of
  `scripts/check_layout.py`.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This is a pure refactor; the change sets `skip_specs: true`. The
allow-list check (users, "Allow-list prefixes are checked") still fails
startup with the same message and exit code; only where the check lives
changes.

## Impact

- Code: every module under `src/alfred/` moves or is renamed. `migrations/env.py`
  follows the moves. Tests are moved to mirror the new packages; no test
  changes what it asserts.
- Dependencies: `import-linter` as a dev dependency.
- Tooling: `make lint` runs `lint-imports`. `scripts/check_layout.py` drops
  its package-cycle check.
- Docs: `AGENTS.md` gets the rank table and the feature order. "Adding a
  tool group" loses the catalog step and follows the new paths. README
  paths are updated.
- Release: nothing deployable changes, so no handover update is needed.
