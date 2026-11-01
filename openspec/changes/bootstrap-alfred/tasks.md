# Tasks

## 1. Agent guardrails and docs skeleton

- [x] 1.1 Add the project rules to the `context:` block of `openspec/config.yaml`: MCP Python SDK v2 only (`mcp>=2,<3`), check every SDK API against the official v2 docs (never v1 examples or memory), and never commit secrets or a real `alfred.toml`. Verify with `openspec instructions proposal --change bootstrap-alfred --json` that `context` contains the rules
- [x] 1.2 Create `AGENTS.md` (what alfred is, those rules, a commands table for `make install/lint/format/test/run/image/push`, and a commit rule: before every commit, follow the `commit-messages` skill) and `CLAUDE.md` containing `@AGENTS.md`. Verify both files exist and `CLAUDE.md` imports `AGENTS.md`
- [x] 1.3 Create `README.md` with an overview, an architecture diagram, and placeholder sections for Configuration, Service API, MCP servers and Deployment, to be filled by later groups. Verify the file exists with those headings

## 2. Project scaffolding

- [x] 2.1 Initialise the uv project (Python 3.13, `src/alfred/` layout per design D2) with dependencies `aiogram`, `fastapi`, `uvicorn`, `httpx`, `mcp>=2,<3`, `sqlalchemy>=2,<3`, `alembic`, `psycopg[binary]>=3,<4`, `pydantic>=2,<3`, `pydantic-settings`, and dev dependencies `pytest`, `pytest-asyncio`, `respx`, `testcontainers[postgres]`, `ruff` (same ruff config as mcparr). Add a `.gitignore` covering `.venv`, `.env` and `alfred.toml`. Verify `uv sync` succeeds and creates `uv.lock`
- [x] 2.2 Add a `Makefile` with `install`, `lint`, `format`, `test` and `run` targets. Verify `make lint` and `make test` pass on the empty project
- [x] 2.3 Add the `alfred` CLI entry point with `serve` and `migrate` subcommands (stubs for now). Verify `uv run alfred --help` lists both

## 3. Configuration

- [x] 3.1 Implement the TOML config models (design D10): file path from `ALFRED_CONFIG` (default `alfred.toml`), every section with its defaults, `extra="forbid"`, name rules (lowercase letters, digits, hyphens), and reserved server name `alfred`. Verify with unit tests for the "Config file location", "All tunables in the file" and "Strict validation" scenarios in `specs/service-configuration/spec.md`
- [x] 3.2 Resolve secrets: `ALFRED_TELEGRAM_BOT_TOKEN`, `ALFRED_DATABASE_URL` (normalised to `postgresql+psycopg://`, as in cookbook), and each `token_env`, stored as `SecretStr`. Collect all problems into one `ConfigError`. Verify with tests for a missing referenced variable, duplicate service tokens, and several problems reported together
- [x] 3.3 Ensure no error or log line contains a secret value. Verify with a test that captures startup error output for invalid configurations containing known secret values and asserts they don't appear
- [x] 3.4 Commit `alfred.toml.example` with every key and its default, and fill the README Configuration section (every TOML key with type, default and description; every `ALFRED_*` variable). Verify with a test that loads `alfred.toml.example` successfully, and check that every config field appears in the README

## 4. Database and history

- [x] 4.1 Implement the SQLAlchemy models and the first alembic migration for `conversations`, `entries`, `queue_items` and `turn_tool_calls` (design D7), including the partial unique index that allows only one current conversation. Wire up `alfred migrate`. Verify with a testcontainers Postgres test that `alfred migrate` creates the schema and that inserting a second current conversation fails
- [x] 4.2 Implement history access: get or create the current conversation, append entries of each kind, read the last N entries oldest first, and map them to model messages with `[notice:<service>]` and `[ask:<service>]` prefixes. Verify with tests for "History survives restart", "Bounded context window" and "Notices are part of the conversation"
- [x] 4.3 Implement the conversation reset (archive the current conversation and start a new one in one transaction; archived entries are kept and never read into context). Verify with tests for "Archived conversations are kept" and that a notice written after a reset lands in the new conversation

## 5. MCP tools

- [x] 5.1 Confirm the MCP SDK v2 client APIs: Streamable HTTP client with a custom `Authorization` header, `list_tools`, `call_tool` and their result types (structured content and content blocks). Record the findings with doc links as an "MCP SDK v2 client notes" section in `AGENTS.md`. Verify the section exists and cites the v2 docs
- [x] 5.2 Implement the per-turn tool registry (design D6): concurrent `list_tools` with `mcp.list_timeout_s`, the unavailable-server list, `<server>_<tool>` naming with cleaning and collision skips, `exclude_tools` filtering with a warning for unknown names, and conversion to OpenAI function definitions. Verify with tests against an in-process MCP SDK v2 server for "Server-prefixed tool names", "Excluded tools" and "Unavailable servers"
- [x] 5.3 Implement tool call routing: look up the routing map, refuse excluded or unknown tools, call with `mcp.call_timeout_s`, convert results to text cut to `ai.tool_result_max_chars`, and turn every error into result text. Verify with tests for a successful call, an excluded tool, an unknown tool, a server error and a truncated result
- [x] 5.4 Fill the README "MCP servers" section: the `[[mcp_servers]]` fields, token setup (an mcparr token with the chosen scope; `exclude_tools` for cookbook's write tools), and how unavailable servers behave. Verify the documented fields match the config model

## 6. Agent loop

- [x] 6.1 Implement the OpenAI-compatible model client over httpx (`{base_url}/v1/chat/completions`, non-streaming, tools, `ai.request_timeout_s`) with the unreachable/failed classification from design D4. Verify with `respx` tests: a content reply, a tool-call reply, a connection error and 503 (unreachable), and a 400 and malformed JSON (failed)
- [x] 6.2 Write the built-in system prompt (Telegram, short and to the point, meaning of the `[notice:]` and `[ask:]` prefixes), the `/ask` `NO_REPLY` instruction, and the unavailable-servers note. The prompt can be overridden with `ai.system_prompt`. Verify with tests that the built-in prompt is used by default, the configured prompt replaces it, and ask turns carry the `NO_REPLY` rule
- [x] 6.3 Implement the turn loop (design D5): local `alfred_*` tools only on owner turns, sequential tool execution, a "side effects possible" flag, `ai.max_requests_per_turn`, and storing each tool call in `turn_tool_calls` and the log. Verify with tests covering every scenario in `specs/agent-loop/spec.md`, using a scripted fake model
- [x] 6.4 Implement `alfred_end_conversation` as a local tool that schedules a reset after the turn, with a strict description. Verify with tests that it is offered on owner turns, never on `/ask` turns, and that calling it marks the turn for reset

## 7. Queue worker

- [x] 7.1 Implement enqueueing (insert `pending`, set the in-process signal) and the worker (design D8): the oldest pending item first, head-of-line retry with `next_attempt_at` and backoff from `queue.retry_initial_s` doubling to `queue.retry_max_s`, no retry once side effects are possible, `failed` with a reason for non-retryable errors, and pending items resuming at startup. Verify with tests covering every scenario in `specs/message-queue/spec.md`
- [x] 7.2 Implement turn completion: record the owner message when its turn starts; record the reply; for `/ask`, record prompt and reply only if the answer isn't `NO_REPLY`, otherwise log the silent run; after delivery, perform any scheduled reset and send `— new conversation —`. Verify with tests for "Ask turns in the conversation" and "Reset after the turn"

## 8. Telegram

- [x] 8.1 Implement the Markdown-to-Telegram-HTML converter and the splitter (design D9). Verify with unit tests for bold, italic, inline and fenced code, links, lists, escaping of `<`, `>` and `&`, a 6000-character reply split without losing content, and a code block crossing a split
- [x] 8.2 Implement the aiogram bot with long polling: owner-only private chat (others ignored and logged by user ID only), text messages queued as written (including `/`-prefixed text), and the fixed "text only" reply for other message types. Verify with tests for "Owner-only access", "No bot commands" and "Text only", using fake updates
- [x] 8.3 Implement sending: HTML send with plain-text fallback when Telegram can't parse it, split messages sent in order, the reply sent for each owner turn, and a short failure notice (no internals) when a turn fails. Verify with tests using a mocked Bot API for "Telegram formatting", "Long replies are split" and "Reply to every owner message"

## 9. Service API

- [x] 9.1 Implement the FastAPI app: service bearer auth (constant-time comparison, 401 for missing, empty or unknown tokens) and an unauthenticated `GET /healthz` that contacts nothing. Verify with ASGI tests for "Service authentication" and "Health endpoint"
- [x] 9.2 Implement `POST /notify` (non-empty `text`, `[<service>]` prefix, sent outside the queue, 200 only after Telegram accepts it, 502 and no history entry on failure, recorded as a notice on success). Verify with tests for "Direct notification" and "Notify delivery result"
- [x] 9.3 Implement `POST /ask` (non-empty `prompt`, enqueue, 202 with item ID) and delivery of non-`NO_REPLY` answers prefixed with the service name. Verify with tests for "Ask the AI" and "AI decides whether to notify"
- [ ] 9.4 Fill the README "Service API" section with request and response formats and curl examples for `/notify` and `/ask`. Verify the examples work against a locally running alfred

## 10. Serve and packaging

- [x] 10.1 Implement `alfred serve`: load config, start the uvicorn FastAPI app, aiogram polling and the worker in one event loop (design D1), with clean shutdown on SIGTERM. Verify locally with `make run` against a test database, a test bot and Ollama that an owner message gets a reply
- [x] 10.2 Write the multi-stage Dockerfile (uv builder, `python:3.13-slim`, non-root user, port 8080, `HEALTHCHECK` on `/healthz`) and `docker-entrypoint.sh` (`alfred migrate`, then exec CMD). Add `make image` and `make push`. Verify `make image` builds `alfred:<version>` and `alfred:latest`, and that the container reports healthy
- [x] 10.3 Fill the README "Deployment" section: create the database, create the bot with BotFather and find your user ID, create MCP tokens, mount `alfred.toml`, list of environment variables, and example run or compose. Verify the steps match the implemented paths, port and variables

## 11. Integration check

- [x] 11.1 Run the image against the real stack (Ollama on `ai.lan`, mcparr, cookbook). From Telegram, verify: a plain question gets a short reply; "what's downloading?" calls `mcparr_get_downloads` and answers with real data; `exclude_tools` hides the configured cookbook tool; saying the conversation is over produces the reply and the `— new conversation —` marker, and the next message has no earlier context
- [ ] 11.2 Verify the service API end to end: `curl /notify` reaches Telegram, and a follow-up question about it is answered with the notice in context; `/ask` with an unremarkable prompt stays silent; `/ask` with a remarkable one reaches Telegram
- [ ] 11.3 Verify the outage behaviour: with the AI unreachable (wrong base URL or server down), send two messages, restore it, and confirm both are answered in order. Tune the default system prompt against the real model if replies are too long, and record the final prompt in the README

## Workflow follow-up

- Rename the project folder to `~/Vault/alfred` (and copy the Claude memory folder) before starting `/opsx:apply`.
- Archive the change with `/opsx:archive` once the integration check passes.
