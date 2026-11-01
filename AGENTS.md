# AGENTS.md

alfred is the owner's two-way channel to the local AI and the homelab services.
It is a Telegram bot (long polling, owner only) and a small HTTP API for
services (`/notify`, `/ask`), with its own agent loop: it calls an
OpenAI-compatible chat completions endpoint (Ollama on `ai.lan` by default),
offers the model the tools of the configured MCP servers, and runs the tool
calls itself. Planning lives in `openspec/`; read the active change
(`openspec/changes/*/`) before implementing.

## Rules

- Use only the MCP Python SDK v2 (`mcp>=2,<3`). Never use SDK v1 APIs.
- Check every SDK API against the official SDK v2 docs. Never rely on v1
  examples, tutorials or memory.
- Never commit secrets, `.env` files, real tokens, or a real `alfred.toml`.
  Only `alfred.toml.example` is committed.

## Commands

| Command        | What it does                                         |
| -------------- | ---------------------------------------------------- |
| `make install` | `uv sync` (creates `.venv`, installs dev deps)       |
| `make lint`    | `ruff check` + `ruff format --check`                 |
| `make format`  | `ruff check --fix` + `ruff format`                   |
| `make test`    | `pytest` (Postgres tests need Docker or Podman)      |
| `make run`     | `alfred serve` locally (needs `alfred.toml` + `ALFRED_*` env) |
| `make image`   | build `alfred:<version>` and `alfred:latest`         |
| `make push`    | build, then push both tags to `album.lan`            |

## Commits

Before every commit, follow the `commit-messages` skill.

## MCP SDK v2 client notes (checked against `mcp` 2.3.0)

Docs: <https://py.sdk.modelcontextprotocol.io/>. alfred is only an MCP
*client*.

- **Connecting with a bearer token.** `mcp.Client(transport)` where
  `transport = streamable_http_client(url, http_client=...)`
  (`mcp.client.streamable_http`). Headers, auth and timeouts live on the
  `httpx2.AsyncClient` passed as `http_client`; `streamable_http_client` has
  no `headers=` argument. We own that client, so we open and close it. A
  client built without `timeout=` gets httpx2's 5-second default.
  [Transports](https://py.sdk.modelcontextprotocol.io/client/transports/)
- **httpx2, not httpx.** The SDK's HTTP transport is built on the `httpx2`
  package (a dependency of `mcp`). The MCP connection must use an
  `httpx2.AsyncClient`; alfred's own model client uses `httpx`.
- **Construction doesn't connect.** `async with Client(...)` opens the
  connection; outside it the client is unusable.
- **No stale tool lists.** `Client` has a response cache that is on by
  default and honours the server's `ttlMs` hints for `tools/list`. alfred
  looks tools up every turn, so it passes `cache=None`.
  [Caching](https://py.sdk.modelcontextprotocol.io/client/caching/)
- **`list_tools()`** returns `ListToolsResult` with `.tools` (each a `Tool`:
  `name`, `description`, `input_schema`) and `.next_cursor` for pagination
  (`list_tools(cursor=...)`).
- **`call_tool(name, arguments)`** returns `CallToolResult`: `content` (a
  list of content blocks; text blocks are `TextContent` with `.text`),
  `structured_content` (any JSON value, or `None`) and `is_error`. An
  exception inside a tool comes back as a normal result with
  `is_error=True`; protocol-level failures (unknown tool, bad request) raise
  `mcp.shared.exceptions.MCPError`. Transport failures raise httpx2 errors.
  [Testing](https://py.sdk.modelcontextprotocol.io/get-started/testing/)
- **Testing.** `Client(server)` with an `mcp.server.MCPServer` instance
  connects in process (no HTTP, no auth). The Streamable HTTP path (and the
  `Authorization` header) is tested through `server.streamable_http_app()`
  with an `httpx2.ASGITransport`; its lifespan needs
  `server.session_manager.run()`, and the default DNS-rebinding guard only
  accepts `localhost` Host headers.
