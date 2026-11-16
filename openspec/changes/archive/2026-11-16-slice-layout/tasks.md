# Tasks

Every group ends with `make lint` and `make test` green. Each group is a
move-only commit (added to `.git-blame-ignore-revs`) followed by its
structural commits (design D8). Tests move with their modules. No test
changes what it asserts.

## 1. Infra and kernel

- [x] 1.1 Add `settings.py` (the `Section` base, the validators and `ConfigError`, from `config.py`); verify `config.py` imports them and `tests/config` passes
- [x] 1.2 Add `times.py` with the time formatting from `reminders/when.py` and `seconds_to_next_check` from `reminders/loop.py`, and repoint `agent`, `tools/dates` and `inputs/loop` at it; verify `grep -rn "reminders.when import format\|reminders.loop import" src/alfred` finds nothing outside `reminders/`
- [x] 1.3 Make `backoff_s` take `initial_s` and `max_s` instead of `QueueConfig`; verify `backoff.py` imports nothing from alfred
- [x] 1.4 Create `llm/`: `chat.py` (from `model.py`), `embedding.py` (from `memory/`), `streaming.py` (one `complete_unless_preempted`, which `Summaries` uses in place of its copy) and `protocols.py` (`ChatModel`, `Embedder`); verify `grep -n "own copy" src/alfred -r` finds nothing and the summary tests pass unchanged
- [x] 1.5 Split `tools/base.py` into `kernel/` (`turn`, `toolset`, `access`, `questions`, `content`), move `User`/`Users` to `kernel/users.py` and `Outbox` to `kernel/outbox.py` (design D2); verify `kernel/` imports nothing from alfred outside `kernel/`, including under `TYPE_CHECKING`

## 2. Tool-name check at startup

- [x] 2.1 Give every `LocalToolset` subclass a `names` class attribute (design D4), replacing the `NAMES`-style constants it duplicates; verify a test asserts each toolset's `definitions()` names are a subset of its `names`
- [x] 2.2 Add `startup.check_tool_names`, called from `app` before any connection opens and raising `ConfigError` with today's message; catch it in `cli` with exit code 2; delete `tools/catalog.py` and the lazy import in `config.py`; verify the moved test asserts the same message, and a test with an unreachable database URL gets the config error
- [x] 2.3 Move `warn_unknown_users` from `db/users.py` to `startup.py`; verify `tests/db/test_users.py` passes from its new place
- [x] 2.4 Remove the catalog step from AGENTS.md's "Adding a tool group" and describe `names`; verify every path the section names exists

## 3. Brains, attachments and toolbox

- [x] 3.1 Create `brains/` (`registry.py` from `brains.py`, `tools.py` from `tools/brains.py`, `config.py` with the brains part of `[ai]`); verify `tests/test_brains.py` and the set-model tests pass from `tests/brains/`
- [x] 3.2 Create `toolbox/` from `tools/calculator.py` and `tools/dates.py`; verify `toolbox` imports only kernel and infra
- [x] 3.3 Move `AttachmentsConfig` and `S3Key` to `attachments/config.py`, and `too_large`, `unreadable` and `not_kept` to `attachments/notices.py`; verify `attachments/` no longer imports `alfred.config` or `alfred.notices`
- [x] 3.4 Give `AttachmentTools` a `ConversationScope` protocol in place of `current_id` and the recall query (design D5), implemented next to the conversation store and wired in `app`; verify `grep -rn "conversations\|Entry" src/alfred/attachments` finds no import, and the attachment tool tests pass with a fake scope

## 4. Conversations

- [x] 4.1 Create `conversations/` from `history.py` (`store.py`), `db/conversations.py` (`current.py`), `idle.py`, `tools/conversation.py`, `memory/transcript.py` plus `load_transcript` (`transcript.py`), with `config.py` (`[history]`) and `notices.py` (`reset_marker`); verify `tests/test_history.py` and `tests/test_idle.py` pass from `tests/conversations/`
- [x] 4.2 Replace the `input_requests` join in the window query with a `hidden_items` argument that the worker fills from `InputRequests` (design D5); verify `conversations/` no longer mentions `InputRequest` or the live statuses, and the parked-turn window tests pass unchanged
- [x] 4.3 Point `ConversationScope` (task 3.4) at `conversations/`; verify `conversations` imports only `attachments`, `brains`, kernel and infra

## 5. Archive and facts

- [x] 5.1 Create `archive/`: `summaries.py`; `summary_job.py` (the summary job, out of `memory/sweeper.py`); `search.py` and `tools.py` (from `memory/search.py`); `prompts.py` (the summary prompt and parser, from `transcript.py`); and `config.py`; verify `tests/memory` summary, search and recall tests pass from `tests/archive/`
- [x] 5.2 Create `facts/`: `store.py`, `embedding_job.py` and `tools.py` (from `memory/facts.py`); `extraction_job.py` (from `memory/extract.py`); `extraction_prompt.py` and `duplicate_prompt.py` (from `memory/extraction.py`); `duplicate_job.py`; `notices.py` (`memory_*`); and `config.py`; verify no module in `facts/` holds more than one of store, job, tools or prompt
- [x] 5.3 Move the sweeper's job runner to `turns/background.py`, taking its jobs from `app`; verify `archive` and `facts` don't import each other or `turns`
- [x] 5.4 Compose `[memory]` from `ArchiveConfig`, `FactsConfig` and the embedding section in `llm/` (design D3), with `summary_model` on the archive section; verify a test loads `alfred.toml.example` and finds every `[memory]` key in its feature's section

## 6. Reminders, web and mcp

- [x] 6.1 Give `reminders/` its `config.py` and `notices.py` (`reminder`), and keep only resolving and occurrences in `when.py`; verify `reminders/` doesn't import `alfred.config` or `alfred.notices`
- [x] 6.2 Give `web/` and `mcp/` their `config.py` (with `servers_for` in `mcp`), and make `WebTools` and `McpTools` take sections instead of `Config`; verify neither imports `alfred.config`

## 7. Runtime and adapters

- [x] 7.1 Create `turns/` from `queue.py`, `worker.py` and `kinds.py`, with `config.py` (`[queue]`, `[input_requests]`) and `notices.py` (`fell_back`); verify `tests/test_queue.py` and `tests/test_worker.py` pass from `tests/turns/`
- [x] 7.2 Move `inputs/` into `turns/inputs/` (`store`, `parking`, `loop`, plus `titles.py` with the plain-text question titles and `notices.py` with `input_not_done`); move the Telegram HTML and keyboard to `telegram/questions.py`, and `callbacks.py` to `telegram/taps.py`, with the expiry loop calling a `QuestionEditor.ended` protocol (design D5); verify `turns/` imports nothing from `telegram`
- [x] 7.3 Split `agent/turn.py` into `turn.py`, `results.py` (`TurnResult`, `TurnFailed`, `Fallback`) and `checkpoint.py`; add `agent/notices.py` and `agent/config.py` (the agent part of `[ai]`); make `Agent` take sections instead of `Config`; verify `agent/` doesn't import `turns` or `alfred.config`
- [x] 7.4 Move `api.py` to `http/api.py` with `http/config.py` (`[server]`, `Service`), and give `telegram/` its `notices.py`; delete `notices.py`; verify `tests/test_notices.py` is split per feature and `ls src/alfred/*.py` shows only `app`, `cli`, `config`, `startup`, `settings`, `times` and `backoff`
- [x] 7.5 Check `config.py` against design D3: only the file model, `Config`, the loaders and the cross-section checks are left, and `grep -rn "from alfred.config import" src/alfred` matches only entry modules

## 8. Enforcement and docs

- [x] 8.1 Add `import-linter` as a dev dependency, with the design D1 contract in `pyproject.toml` (`exhaustive = true`, type-checking imports counted), and run `lint-imports` from `make lint`; verify it passes, and fails on a deliberate upward import, which is then removed
- [x] 8.2 Drop check 5 from `scripts/check_layout.py`; verify `make lint` still passes and the script's docstring no longer mentions package cycles
- [x] 8.3 Add the rank table and feature order to AGENTS.md ("Code layout"), describe what a feature package holds (domain, store, tools, jobs, config, notices), and update every path it names; verify each named path exists
- [x] 8.4 Update README paths that name moved modules; verify `grep -rnE "alfred/(history|queue|worker|kinds|notices|memory|tools)/?[a-z_]*\.py" README.md AGENTS.md` finds nothing
- [x] 8.5 Add the hashes of every move-only commit to `.git-blame-ignore-revs`; verify `git blame` on a moved file shows the original commits
