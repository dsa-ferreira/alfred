# AGENTS.md

alfred is its users' two-way channel to the local AI and the homelab services.
It is a Telegram bot (long polling, configured `[[users]]` only, one of them
the admin) and a small HTTP API for services (`/notify`, `/ask`), with its
own agent loop: it calls an OpenAI-compatible chat completions endpoint
(Ollama on `ai.lan` by default), offers the model the tools of the
configured MCP servers, and runs the tool calls itself. Planning lives in `openspec/`; read the active change
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
| `make lint`    | `ruff check` + `ruff format --check` + layout check  |
| `make format`  | `ruff check --fix` + `ruff format`                   |
| `make test`    | `pytest` (Postgres tests need Docker or Podman)      |
| `make run`     | `alfred serve` locally (needs `alfred.toml` + `ALFRED_*` env) |
| `make image`   | build `alfred:<version>` and `alfred:latest`         |
| `make push`    | build, then push both tags to `album.lan`            |

## Adding a tool group

Everything the model can call is a toolset. The contract lives in
`alfred.tools.base`, which imports nothing else from alfred; the turn loop in
`agent/turn.py` names no tool. To add one:

- Subclass `LocalToolset` in the feature's own package, or in `alfred/tools/`
  if it belongs to no feature (like `conversation.py` and `brains.py`). Set
  `definitions`, implement `call(name, raw_arguments, attempt)`, and
  override `offered` if it is not for every turn (`chat_only` for turns
  started by a user's chat message). A turn is a `ChatTurn`, `AskTurn` or
  `ReminderTurn` (`TurnInput` is their union); tell them apart with
  `isinstance`, never by a kind string. Override `open` / `new_attempt`
  only for per-turn or per-attempt state (see `web/tools.py`).
- Don't check the user's tool allow-list or confirm list yourself: `turn.py`
  wraps every opened toolset in `Confirmed(Gated(...))` (`alfred.tools.base`).
  `Gated` hides and refuses the names the turn user's `AllowList` doesn't
  match; `Confirmed` holds back the calls their confirm list matches until
  they tap **Do it**. Only prompt notes about tools the list hides need care
  (see `attachments/tools.py`).
- A toolset that needs the user's answer mid-call returns
  `ToolOutcome.waiting(InputNeed(question, state))` and implements
  `resume(name, raw_arguments, state, answer, attempt)`: the turn is parked,
  and once the user taps an option, `resume` gets the call again with that
  option's `answer` (see `mcp/toolset.py`). `state` and `answer` must be
  JSON-able: they are stored while the turn waits. `LocalToolset.resume`
  raises `NotImplementedError`, which becomes an error result.
- Whose turn it is is `turn.user` (`attempt.turn.user` in `call`). Data a
  tool reads or writes belongs to that user; filter by `user.name` in the
  query, never in the prompt.
- Add the tool's name to `alfred/tools/catalog.py`, so exact
  `alfred_*` allow-list entries naming it pass the startup check.
- Add it to `_build_toolsets` in `app.py`. The list order is the order the
  tools are offered in; alfred's own tools come before `McpTools`.
- Exceptions from `call` become error results for the model (`_safe_call`).
- Test the toolset alone, and a whole turn with `tests/agent/turn_helpers.py`.

## Code layout

Every module under `src/alfred/` and `migrations/` follows one order, so a
reader can tell from a name and its position whether other modules use it:

```
module
  imports (TYPE_CHECKING block included)
  constants and variables      log, type aliases, TypeVars, public and _private constants
  public supporting types      results, exceptions, protocols, small dataclasses
  public main items            the module's main class(es) or function(s)
  _internal items              classes and functions used only in this module

class
  class attributes (dataclass fields included)
  __init__ and other dunders
  public methods
  _private methods
```

- **Public** means used by another module: anything under `src/alfred/`,
  anything under `migrations/`, or an entry point in `pyproject.toml`
  (`alfred.cli:run`). Tests don't count. A name is public exactly when it
  has no leading `_`; a name only its own module uses starts with `_`.
- Tests may import `_` names where they need to.
- **Python's definition order wins.** A `_` item that a later item needs at
  import time (a base class, a decorator, a default value, a
  `default_factory`, something called at module level) goes directly above
  its first such user, not in the internal section. A variable that needs a
  public class or function at import time (`X = A | B`) goes in the public
  section, below what it needs.
- A method is public when something outside its class calls it, including
  through a protocol (`Toolset.open`, `call`, `resume`). Method names follow
  the same `_` rule.

`scripts/check_layout.py` (run by `make lint`) checks the parts that can be
checked mechanically. It can't tell whether a method is called outside its
class, or which public item is the main one; review covers those.

## Commits

Commits that only rename, move or reword code are listed in
`.git-blame-ignore-revs`; add yours there. Run
`git config blame.ignoreRevsFile .git-blame-ignore-revs` once so `git blame`
skips them.

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
- **Servers asking the user (protocol 2026-07-28).** A server may answer
  `tools/call` with `InputRequiredResult` (`input_requests` keyed by the
  server, plus an opaque `request_state`). alfred drives this itself:
  - The capability is declared by passing `elicitation_callback` to `Client`
    (`mcp/client.py` `new_client`); without one the server fails the call
    with "did not declare the form elicitation capability". The SDK declares
    form and URL mode together; alfred refuses URL requests itself.
  - `client.session.call_tool(..., allow_input_required=True)` returns the
    `InputRequiredResult` instead of running the callback loop of
    `Client.call_tool`. The answer goes back on a *fresh* connection with
    `input_responses={key: ElicitResult(...)}` and the same `request_state`
    and arguments.
  - The server refuses a stale `request_state` (TTL, 600 s by default; a
    restart with the default process-local key; changed arguments) with
    `MCPError` `-32602`. Leaving the `Client` context wraps it in an
    exception group.
  - **The same `request_state` can be replayed, and the tool then runs
    again.** Only alfred's own status changes stop a second answer.
  - The session-level call skips `Client.call_tool`'s recovery from
    `HEADER_MISMATCH` (`-32020`). Checked over Streamable HTTP: a fresh
    connection that hasn't listed the tool gets it only for tools with
    `x-mcp-header` arguments (the client can't send their `Mcp-Param-*`
    headers). alfred re-lists and resends once, as the SDK does.
- **Testing.** `Client(server)` with an `mcp.server.MCPServer` instance
  connects in process (no HTTP, no auth). The Streamable HTTP path (and the
  `Authorization` header) is tested through `server.streamable_http_app()`
  with an `httpx2.ASGITransport`; its lifespan needs
  `server.session_manager.run()`, and the default DNS-rebinding guard only
  accepts `localhost` Host headers.
