# Design

## Context

This is a new project; the motivation is in proposal.md, and behaviour is defined in `specs/`.

Facts established before this change (tested on 2026-11-01 against Open WebUI 0.12.5 and `gemma4:12b-it-qat`):

- Open WebUI's `/api/chat/completions` with `tool_ids` returns `tool_calls` to the caller instead of running MCP tools. Server-side execution only happens over its Socket.IO background-task flow. That is why alfred runs its own tool loop and does not use Open WebUI.
- The model chose the right MCP tool (`mcparr_get_downloads`) from a natural request, and correctly used a notice placed earlier in the history as context.
- The AI server sleeps; a proxy in front of `ai.lan` wakes it on request. The first request after sleep can take tens of seconds or more. alfred only needs long timeouts.

Sibling projects (cookbook, mcparr) set the conventions alfred follows: Python 3.13 with uv, a `src/` layout, pydantic-settings, MCP Python SDK v2 (`mcp>=2,<3`), SQLAlchemy 2 with alembic and psycopg 3, pytest (testcontainers for Postgres), ruff, a multi-stage Dockerfile with a non-root user and a `/healthz` healthcheck, a Makefile, and an `AGENTS.md` that `CLAUDE.md` imports.

## Goals / Non-Goals

**Goals:**
- One small, single-instance process that owns the Telegram bot, the service API and the AI worker.
- Every behaviour-affecting number and endpoint in `alfred.toml`.
- Never repeat a tool action because of a retry.

**Non-Goals:**
- Running several instances. The single worker and the Telegram long-polling lock assume one replica.
- Streaming replies, typing or progress indicators.
- More than one Telegram user.
- Images, voice or files.

## Decisions

### D1. One process, one event loop
alfred runs as one asyncio process with three tasks: aiogram long polling, the FastAPI app under uvicorn, and the queue worker. They share the database engine and an in-process "work available" signal.
- *Why:* All three are I/O-bound and small. A single process removes any need for distributed locking around the single worker.
- *Alternative:* separate processes for the bot, API and worker. More to deploy, and the "one turn at a time" guarantee would need a database lock.

### D2. Project layout
`src/alfred/` with modules for `config`, `db` (models and migrations), `telegram` (bot, formatting, splitting), `api` (FastAPI routes and service auth), `queue` (worker and retry), `agent` (model client and turn loop), `mcp` (server registry and tool routing), `history` (conversation access) and `cli` (`alfred serve`, `alfred migrate`). Tests mirror this layout.

### D3. Single owner, not an allowlist
The configuration has one `telegram.owner_user_id` (an integer), not a list of allowed IDs.
- *Why:* There is one current conversation, and `/notify` and `/ask` need exactly one recipient. A list would raise questions about which user gets notices and whose history is shared, and v1 has no answers to those.
- *Alternative:* `allowed_user_ids = [...]`, as sketched during exploration. That's a later change if a second user is ever needed. **This differs from the exploration sketch; confirm during review.**

### D4. Model client: OpenAI-compatible API over httpx
alfred posts to `{ai.base_url}/v1/chat/completions` with `stream: false`, the `messages`, and OpenAI-style `tools` (function name, description, JSON-schema parameters). It reads `choices[0].message.content` and `tool_calls`.
- *Why:* The owner chose this so model servers can be swapped. Ollama serves the endpoint natively.
- *Alternative:* the `ollama` Python client. It's Ollama-specific.
- How failures are classified (which drives retrying):

  | Outcome | Meaning |
  |---|---|
  | connection error, connect/read timeout, 502/503/504, other 5xx | **unreachable** (retryable if no tool has run yet in the turn) |
  | 4xx, malformed JSON, missing `choices` | **failed** (not retried) |

### D5. Turn loop
```
tools = alfred_* local tools (owner turns only) + tools of reachable MCP servers
msgs  = [system prompt (+ unavailable servers note, + NO_REPLY rule for /ask)]
        + last N history entries + new input
loop up to ai.max_requests_per_turn:
    resp = model(msgs, tools)
    if no tool_calls: return resp.content
    for call in resp.tool_calls (in order):
        result = run(call)   # errors become result text, never exceptions
        mark turn "side effects possible" once any tool has run
        append assistant tool_call msg + tool result msg
fail "stopped after too many steps"
```
- Tool calls in one response run **one after another** to keep ordering predictable. Writes are rare, so parallelism isn't worth it.
- Results are converted to text (structured content as JSON if present, otherwise the text content blocks joined) and cut to `ai.tool_result_max_chars` with a "[truncated]" note, so one large result can't overflow a 12B model's context.
- "Side effects possible" is set conservatively: any tool call, read or write. Telling read and write tools apart would need per-tool metadata that MCP servers don't reliably provide.

### D6. MCP client: SDK v2, looked up per turn
For each configured server, the turn opens a Streamable HTTP client with an `Authorization: Bearer <token>` header and calls `list_tools`. Lookups run concurrently with a short timeout (`mcp.list_timeout_s`). A server that errors is listed in the "unavailable" note for that turn.
- The routing map `prefixed name -> (server, original name)` is built per turn, so prefix parsing never has to guess (server names may contain hyphens).
- Tool names sent to the model are cleaned to the allowed `[a-zA-Z0-9_-]{1,64}`. A name that would collide or exceed the limit after cleaning is skipped with a warning.
- *Why per turn:* stateless MCP makes `list_tools` cheap, and it handles servers going up and down without restarting alfred.
- *Alternative:* cache at startup and refresh on failure. Faster, but stale and more stateful.
- The exact SDK v2 client APIs (Streamable HTTP client, headers, `list_tools`/`call_tool` result types) must be checked against the official v2 docs before writing code, as in mcparr. Nothing may be written from v1 examples.

### D7. Data model (Postgres, alembic)
```
conversations  id, started_at, ended_at (null = current)        -- at most one current (partial unique index)
entries        id, conversation_id, kind(owner|assistant|notice|ask_prompt|ask_reply),
               source (service name or null), text, created_at
queue_items    id, kind(owner|ask), source, text, telegram_message_id (owner),
               status(pending|done|failed), attempts, next_attempt_at,
               failure_reason, created_at, finished_at
turn_tool_calls id, queue_item_id, seq, tool, arguments(jsonb), ok, result_excerpt, created_at
```
- The context window reads the newest `history.max_entries` rows of `entries` for the current conversation, oldest first. They map to model roles: owner and `ask_prompt` become `user` (with `[ask:<service>]` added to ask prompts), assistant and `ask_reply` become `assistant`, and notices become `user` messages prefixed `[notice:<service>]`. The system prompt explains both prefixes.
- **Reset:** within one transaction, set `ended_at` on the current conversation and insert a new one. This happens after the reply is sent, then the marker is sent. The ID of the "current" conversation is read when each entry is written, so a notice that races with a reset lands in whichever conversation is current at commit time.
- **Owner turn recording:** the owner's message is added to `entries` when its turn starts, not when it is queued. Otherwise a queued message would appear in its own context twice, or before earlier turns' replies.

### D8. Queue worker
- Enqueue = insert a `pending` row, then set the in-process signal. `/ask` returns the row ID with 202.
- The worker takes the oldest `pending` row whose `next_attempt_at` is in the past. On an unreachable AI (no tool run yet), it increments `attempts`, sets `next_attempt_at = now + min(initial * 2^(attempts-1), max)`, and keeps the item at the head: later items aren't taken while an earlier one is pending.
- At startup, any rows still `pending` simply resume. There is no "in progress" state to recover, because a turn interrupted by a crash is retried. This is the one case where a tool might run twice. See Risks.
- *Alternative:* `SELECT ... FOR UPDATE SKIP LOCKED`. Not needed with one worker in one process.

### D9. Telegram formatting: Markdown to Telegram HTML
The model writes Markdown. alfred converts a small subset (bold, italic, inline code, fenced code, links, bullet lists, and headings as bold) into Telegram's HTML parse mode, escaping everything else. On a Telegram "can't parse entities" error, it resends as plain text.
- *Why HTML:* only `<`, `>` and `&` need escaping. MarkdownV2 needs about 18 characters escaped and breaks often on model output.
- *Alternative:* a third-party converter library. It's a small amount of code and keeps dependencies down; this can be revisited if edge cases pile up.
- Splitting happens before formatting, on the Markdown source, at blank lines, then newlines, then a hard cut, with a limit a little below 4096 to leave room for tags. A fenced code block that crosses a split is closed and reopened.

### D10. Configuration
pydantic models loaded from TOML with `tomllib` and `extra="forbid"`. Secrets are resolved from `ALFRED_*` and `token_env` variables and kept as `SecretStr`. All errors are collected into one `ConfigError`, as in mcparr and cookbook. The default `alfred.toml.example` is committed; a real `alfred.toml` is git-ignored.

```toml
[telegram]
owner_user_id = 123456789

[ai]
base_url = "http://ai.lan"
model = "gemma4:12b-it-qat"
request_timeout_s = 300
max_requests_per_turn = 8
tool_result_max_chars = 4000
# system_prompt = """..."""      # optional, replaces the built-in

[history]
max_entries = 30

[queue]
retry_initial_s = 30
retry_max_s = 900

[mcp]
list_timeout_s = 10
call_timeout_s = 60

[server]
host = "0.0.0.0"
port = 8080
log_level = "INFO"

[[services]]
name = "backup"
token_env = "ALFRED_SERVICE_TOKEN_BACKUP"

[[mcp_servers]]
name = "mcparr"
url = "http://192.168.0.50:8090/mcp"
token_env = "ALFRED_MCP_TOKEN_MCPARR"
exclude_tools = []
```
The defaults above are starting values; the owner can change any of them in the file.

### D11. Packaging
The same pattern as cookbook and mcparr: a multi-stage uv Dockerfile, a non-root user, `EXPOSE 8080`, and a `HEALTHCHECK` on `/healthz`. `docker-entrypoint.sh` runs `alfred migrate` and then execs `alfred serve`. Makefile targets are `install`, `lint`, `format`, `test`, `run`, `image` and `push`.

### D12. Docs and agent guardrails
- `README.md`: what alfred is, the configuration reference (every TOML key with its default, and every environment variable), the `/notify` and `/ask` API with curl examples, MCP server setup (including giving alfred an mcparr token and using `exclude_tools` for cookbook), and deployment.
- `AGENTS.md`: project rules (MCP SDK v2 only, checked against the official docs; never commit secrets or `alfred.toml`), commands, and the commit rule (follow the `commit-messages` skill), plus SDK notes recorded while implementing.
- `CLAUDE.md`: contains `@AGENTS.md`.
- The same rules go into the `context:` of `openspec/config.yaml`.

## Risks / Trade-offs

- [A crash during a turn after a tool ran causes the turn to be retried at startup and the tool to run again.] → Accepted for v1. The window is small, mcparr's `add_media` is idempotent ("already present" is a no-op), and every tool call is logged. A later change could record "tools started" before running a tool and refuse to retry those items.
- [A 12B model calls tools poorly or ends conversations too eagerly.] → A strict tool description, the visible reset marker, `max_requests_per_turn`, and a configurable system prompt and model.
- [A long outage leaves a large queue that runs all at once when the AI returns.] → Accepted; items still run one at a time and in order. Stale `/ask` items may be less useful but stay harmless.
- [Head-of-line blocking: a retrying item holds up everything behind it.] → Intended (it keeps order). `retry_max_s` limits the gap between attempts.
- [Listing tools from every MCP server on every turn adds latency.] → Lookups run concurrently with a short timeout. That's small next to model latency.
- [Telegram formatting edge cases.] → Plain-text fallback, so content is never lost.
- [Tool results in transcripts may contain personal data.] → Only an excerpt is stored, in the owner's own database.

## Migration Plan

New service, nothing to migrate. Deployment: create the `alfred` database on the shared Postgres, create the bot with BotFather, give alfred tokens on mcparr (scope chosen by the owner) and cookbook, write `alfred.toml`, set the `ALFRED_*` secrets, and run the image. The entrypoint applies migrations. To roll back, stop the container; no other system depends on it except services calling `/notify` or `/ask`, which get connection errors.

## Open Questions

- The default system prompt wording gets tuned against the real model during the integration check. It's configurable, so this doesn't affect the specs.
