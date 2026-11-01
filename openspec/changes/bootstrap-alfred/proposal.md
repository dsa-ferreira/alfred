# Proposal

## Why

There is no single place to talk to the local AI and the homelab services from a phone. The AI (Ollama on `ai.lan`) can already reach services through MCP servers (mcparr, cookbook), but only through Open WebUI on the LAN, and services have no way to send a message to the user. Open WebUI's API returns MCP tool calls to the caller instead of running them, so a Telegram front end cannot be a thin relay. It needs its own small agent loop.

## What Changes

- New service **alfred**: a Telegram bot (long polling, restricted to allowed Telegram user IDs) that is the user's two-way channel to the local AI and the homelab services. There are no bot commands; every user message goes to the AI.
- alfred runs its own agent loop against an OpenAI-compatible chat completions endpoint (Ollama at `ai.lan` by default, switchable through configuration). It gives the model the tools of the configured MCP servers plus alfred's own local tools, and runs the tool calls itself.
- An HTTP API for services:
  - `POST /notify` sends a message directly to the user without involving the AI, and records it in the conversation so the AI can see it.
  - `POST /ask` sends a prompt through the AI, which decides whether the user needs to hear about it. A `NO_REPLY` answer means no message to the user.
- Messages to the AI (Telegram and `/ask`) go into one persisted FIFO queue with a single worker. When the AI is unreachable, they wait and are retried rather than being lost.
- Conversation history is stored in Postgres. The AI sees the last N messages of the current conversation. The user can end a conversation in plain language; the AI calls a local `end_conversation` tool, the old conversation is archived, and alfred posts a visible `— new conversation —` marker.
- Replies are kept short and suited to Telegram through a system prompt and conversion to Telegram formatting.
- All non-secret settings (model endpoint, limits, timings, MCP server list, excluded tools) live in one `alfred.toml` file. Secrets are only accepted from `ALFRED_*` environment variables. Everything is validated at startup.
- Project scaffolding: `uv` Python 3.13 project, Postgres migrations, Dockerfile, Makefile, plus `README.md` and `AGENTS.md` (with `CLAUDE.md` importing it).

Out of scope for this change: asking for confirmation before running risky tools, typing or progress indicators, scheduling (it stays outside alfred), waking the AI server (handled upstream), and using Open WebUI.

## Capabilities

### New Capabilities

- `telegram-chat`: The Telegram bot: long polling, allowed users, sending user messages to the AI and replies back, reply length and Telegram formatting.
- `service-api`: The HTTP API for services: `/notify` and `/ask`, per-service tokens, `NO_REPLY` handling, health endpoint.
- `message-queue`: Ordered processing of AI-bound messages with a single worker, persistence across restarts, and retry while the AI is unreachable.
- `agent-loop`: Running one AI turn against an OpenAI-compatible endpoint: system prompts, the tool-call loop, limits and timeouts.
- `mcp-tools`: Connecting to configured MCP servers, exposing their tools with server-prefixed names, `exclude_tools`, and handling unavailable servers.
- `conversation-history`: Storing conversations and messages, the last-N context window, recording notices, and ending a conversation in natural language with a visible marker.
- `service-configuration`: The `alfred.toml` file plus `ALFRED_*` secret environment variables, validated at startup without leaking secrets.

### Modified Capabilities

None (new project).

## Impact

- New Python service in this repository (`src/alfred/`), with its own Docker image.
- New dependencies: `aiogram`, `fastapi`, `uvicorn`, `httpx`, `mcp>=2,<3` (client), `sqlalchemy>=2`, `alembic`, `psycopg[binary]>=3`, `pydantic`, `pydantic-settings`; dev dependencies `pytest`, `pytest-asyncio`, `respx`, `testcontainers[postgres]`, `ruff`.
- Needs a Postgres database (its own database on the shared homelab Postgres server), a Telegram bot token, network access to `ai.lan` and the MCP servers, and outbound access to the Telegram API.
- MCP servers: alfred is a new MCP client. mcparr should get an alfred token with the scope the user chooses; cookbook has no scopes, so its write tools are limited with `exclude_tools`.
- Services that want to message the user call alfred's `/notify` or `/ask` with their own tokens.
