# Tasks

Every group ends with `make lint` and `make test` green. No test changes
what it asserts.

## 1. Move

- [x] 1.1 Create `adapters/`, `runtime/`, `features/` and `infra/` with a one-line `__init__.py` each, and `git mv` the packages and the loose infra modules into them (design D1); verify `ls src/alfred` shows only the entry modules, `kernel/` and the four rank packages
- [x] 1.2 Rewrite every `alfred.<pkg>` reference to `alfred.<rank>.<pkg>` in src, tests, migrations and scripts, including mock targets and logger names in strings (design D3), then run `make format`; verify `grep -rnE "alfred\.(telegram|http|turns|agent|archive|facts|reminders|web|mcp|toolbox|conversations|attachments|brains|db|llm|backoff|times|settings)\b" src tests migrations scripts` finds nothing, and `make test` passes
- [x] 1.3 Move the tests to mirror the new tree (`tests/features/facts/`, `tests/infra/db/`, `tests/infra/test_times.py`, ...), keeping `conftest.py`, `fakes.py` and `helpers.py` at `tests/`; verify every test directory matches a package and the test count is unchanged
- [x] 1.4 Run `scripts/check_layout.py` on the new tree; verify it reports nothing, and still flags a deliberate `_` name imported from another module, which is then removed
- [x] 1.5 Commit the move alone and add its hash to `.git-blame-ignore-revs`; verify `git blame` on a moved file shows the original commits

## 2. Contracts and docs

- [x] 2.1 Replace the import-linter contract with the rank contract and one contract per rank package (design D2), all exhaustive; verify `lint-imports` passes, and fails both on a deliberate `features` -> `runtime` import and on a new package left out of its rank's contract, which are then removed
- [x] 2.2 Rewrite AGENTS.md's "Packages and ranks" section for the tree (where a new package goes, the per-rank contracts) and update every path in AGENTS.md and README; verify every `src/alfred/...` path they name exists
