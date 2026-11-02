# Tasks

## 1. Database and dependencies

- [x] 1.1 Add the `pgvector` and `tzdata` Python packages to `pyproject.toml` and run `make install`; verify `python -c "import pgvector.sqlalchemy, zoneinfo; zoneinfo.ZoneInfo('Europe/Lisbon')"` works in `.venv`
- [x] 1.2 Switch the test Postgres container in `tests/conftest.py` to a pinned pgvector build of Postgres 17 (current tag from Docker Hub) and run `CREATE EXTENSION vector` as its superuser before migrating (design D2); verify the existing suite still passes with `make test`
- [x] 1.3 Write migration `migrations/versions/0002_conversation_memory.py` and update `src/alfred/db/models.py`: check for the `vector` extension and stop with a clear message if it is missing; add the new `conversations` columns, the generated `search_tsv` column and its GIN index, the `entries` full-text GIN index, the `recall` entry kind, `recalled_conversation_id` and the partial unique index (design D1, D2, D8); write a working `downgrade()`; extend `tests/db/test_schema.py` to check that the models match the migrated schema, that the migration fails clearly without the extension, and that upgrade, downgrade and upgrade again succeed; verify with `make test`

## 2. Configuration

- [x] 2.1 Add `MemoryConfig` (`[memory]`, design D11) to `src/alfred/config.py` and `Config`, with `summary_model` defaulting to `ai.model`; in `tests/config/test_config.py` check the defaults, custom values, an invalid URL and non-positive caps being rejected, and unknown keys under `[memory]` being rejected; verify with `make test`
- [x] 2.2 Add `[ai] timezone` (default `Europe/Lisbon`, validated with `zoneinfo`, design D11); in `tests/config/test_config.py` check the default, a custom zone, and that `Europe/Lisboa` is rejected with an error naming `ai.timezone`; verify with `make test`
- [x] 2.3 Document `[memory]` and `[ai] timezone` in `alfred.toml.example` with one-line comments; verify `tests/config/test_example.py` passes

## 3. Summaries and embeddings

- [x] 3.1 Add an embedding client (`POST {base}/v1/embeddings`, design D6) with errors classified like `ModelClient`'s; test success, the prefix being applied, connection errors, 5xx and malformed replies with an `httpx.MockTransport`; verify with `make test`
- [x] 3.2 Add transcript rendering for the summariser (recalls shown by title only) and the first-and-last-halves cut at `summary_input_max_chars` (design D5); unit-test both the rendering and the cut with its marker; verify with `make test`
- [x] 3.3 Add the summary prompt and the lenient `Title:`/`Summary:` parser (design D5); test bold, quotes, mixed case, a missing title line (first five words used) and a reply with neither line (failure); verify with `make test`
- [x] 3.4 Add `Memory.summarise(conversation_id)`: one model request, one embedding request, storing title, summary, embedding, `embedding_model` and `summarised_at`; on an embedding failure keep the title and summary and return the title; on a model failure return `None` and schedule a retry (`summary_attempts`, `summary_next_at`); test each path against the test database with fake model and embedding clients; verify with `make test`

## 4. Reset sequence and marker

- [x] 4.1 Make `History.reset()` return the archived and the new conversation ids; update its callers and `tests/test_history.py`; verify with `make test`
- [x] 4.2 In `src/alfred/queue.py`, after a reset, call `Memory.summarise` and send the marker with ids and title, or ids only when there is no title, as one message (design D3); in `tests/test_queue.py` check: the reply, then the marker with `#41`, the title and `new conversation #42`; the ids-only marker when summarising fails; a notice recorded during the summary lands in the new conversation; verify with `make test`

## 5. Background sweeper

- [x] 5.1 Add the sweeper task (design D4): archived conversations that are due and missing a summary, or an embedding for the configured model, processed oldest first with queue-style backoff; it sleeps until the next due row and is nudged after a reset; test that it backfills a conversation without a summary, retries with backoff after a failure, re-embeds rows made with another model, and that owner items in the queue are processed while it is failing; verify with `make test`
- [x] 5.2 Start the sweeper in `serve` (`src/alfred/app.py`) alongside the other tasks, with the same stop-everything-on-exit behaviour; extend `tests/test_serve.py` to check that it starts and stops with the others; verify with `make test`

## 6. Search and recall

- [x] 6.1 Add `Memory.search(query)` (design D7): vector candidates for the configured model, keyword candidates from `search_tsv` and entry text, reciprocal rank fusion, `search_results` limit, current conversation excluded, `best`/`close`/`weak` labels, keyword-only results plus a note when embedding fails; test with seeded conversations: a match by meaning, a keyword found only in an entry, the exclusion, the limit and the ragman-down fallback; verify with `make test`
- [x] 6.2 Add the `after`/`before` date filter and date-only search to `Memory.search` (design D7): local dates in `ai.timezone`, overlap of the conversation's span with the range, newest first without a query, an error result with neither a query nor a date, an error result for an unparseable date, and result dates formatted in `ai.timezone`; test topic + range (only the in-range chicken conversations), a conversation spanning the range edge, a conversation ending just after local midnight in Lisbon, date-only ordering, and both errors; verify with `make test`
- [x] 6.3 Add `Memory.recall(conversation_id)` (design D8): a rendered transcript capped at `recall_max_chars` (summary + `[earlier part omitted]` + the end when too long), a `recall` entry written in the current conversation under the current-conversation lock, and error results for an unknown id, the current conversation and a duplicate; test each case, including the cap; verify with `make test`
- [x] 6.4 Return recall data from `History.recent()` and render recall entries in `to_messages()`: the newest in full, older ones as title + summary, and recalls counting as one window entry (design D9); test both renderings and the window count in `tests/test_history.py`; verify with `make test`

## 7. Agent tools and prompt

- [x] 7.1 Define `alfred_search_conversations` (optional `query`, `after`, `before`) and `alfred_recall_conversation` with the descriptions from design D10, offer them only in owner turns, dispatch them in `Agent._run_tool` to `Memory`, and record them in `turn_tool_calls`; in `tests/agent/test_turn.py` check the tools are offered in owner turns and not in `/ask` turns, a recall followed by a reply leaves a recall entry, and the next turn's context has the `[recall:#41 …]` message; verify with `make test`
- [x] 7.2 Add the recall paragraph to `OPERATING_RULES` in `src/alfred/agent/prompts.py`; extend `tests/agent/test_prompts.py` to check it is present with the default and with a custom persona; verify with `make test`
- [x] 7.3 Add today's date line (design D12): `system_prompt()` takes a date and appends `Today is <Weekday>, <D> <Month> <YYYY>.` to the operating rules; `Agent.run` passes today's date in `ai.timezone` for owner and `/ask` turns; extend `tests/agent/test_prompts.py` (exact line, no time of day, present with a custom persona) and `tests/agent/test_turn.py` (an injected clock at 00:30 Lisbon on 3 Nov 2026 gives Tuesday, 3 November 2026 in both turn kinds); verify with `make test`
- [x] 7.4 Wire `Memory`, the embedding client and the optional summary model client into `serve` and `Agent`, closing the new HTTP clients on shutdown; verify with `make test`

## 8. Integration and release

- [x] 8.1 Run `make lint` and `make test`; both pass
- [x] 8.2 Manual check against the real services (`make run` with a local pgvector Postgres): talk about a recipe, end the conversation and confirm the marker shows `#id` and a sensible title; start a new conversation, ask "remember when we talked about the recipe?" and confirm the log shows `alfred_search_conversations` then `alfred_recall_conversation`, and that a follow-up question is answered from the recalled transcript; say "remember conversation <id>" and confirm a direct recall; ask "what did we talk about today?" and confirm the search call has today's date as `after`/`before` and no query; stop ragman access (wrong `embedding_base_url`) and confirm search still finds the conversation by keyword
- [x] 8.3 Bump the version to 0.4.0 in `pyproject.toml`; verify `make image` tags `alfred:0.4.0`
- [x] 8.4 Write the handover update with the `homelab-handover` skill: new image tag; Postgres image switched to the pinned pgvector build; `CREATE EXTENSION IF NOT EXISTS vector` in the init script, plus a one-time run as superuser on the existing database; the `[memory]` keys and `[ai] timezone` with the recommended values and a short reason for each; `ragman.lan` as a new dependency reached over HTTP; the rollback steps from design's Migration Plan; verify the update file exists under `handover/updates/`

## Workflow follow-up

- Archive the change with `/opsx:archive` once implemented and deployed.
