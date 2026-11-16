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
| `make lint`    | `ruff check` + `ruff format --check` + layout + import contracts |
| `make format`  | `ruff check --fix` + `ruff format`                   |
| `make test`    | `pytest` (Postgres tests need Docker or Podman)      |
| `make run`     | `alfred serve` locally (needs `alfred.toml` + `ALFRED_*` env) |
| `make image`   | build `alfred:<version>` and `alfred:latest`         |
| `make push`    | build, then push both tags to `album.lan`            |

## Adding a tool group

Everything the model can call is a toolset. The contract lives in
`alfred.kernel` (`toolset.py`, with turns in `turn.py` and the allow and
confirm lists in `access.py`), which imports nothing else from alfred; the
turn loop in `agent/turn.py` names no tool. To add one:

- Subclass `LocalToolset` in the feature's own `tools.py` (see "Packages
  and ranks"), or in `alfred/toolbox/` if it belongs to no feature (like
  `calculator.py` and `dates.py`). Set `names` (every name it can ever
  offer) and `definitions`, implement `call(name, raw_arguments, attempt)`,
  and override `offered` if it is not for every turn (`chat_only` for turns
  started by a user's chat message). A turn is a `ChatTurn`, `AskTurn` or
  `ReminderTurn` (`TurnInput` is their union, plain data in
  `kernel/turn.py`); a toolset tells them apart with `isinstance` in
  `offered`, `open` or `new_attempt`, never by a kind string. Everything
  else about a kind is its `TurnKind`'s (see "Turn kinds"). Override
  `open` / `new_attempt` only for per-turn or per-attempt state (see
  `web/tools.py`).
- Don't check the user's tool allow-list or confirm list yourself: `turn.py`
  wraps every opened toolset in `Confirmed(Gated(...))` (`alfred.kernel.access`).
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
- Add it to `_build_toolsets` and `_TOOLSET_TYPES` in `app.py`, in the
  same place in both. The list order is the order the tools are offered in;
  alfred's own tools come before `McpTools`. At startup,
  `startup.check_tool_names` checks exact `alfred_*` allow-list entries
  against the `names` of `_TOOLSET_TYPES`.
- Exceptions from `call` become error results for the model (`_safe_call`).
- Test the toolset alone, and a whole turn with `tests/agent/turn_helpers.py`.

## Turn kinds

A turn answers a user's chat message, a service's `/ask` prompt or a due
reminder. Each kind is one module in `alfred/turns/kinds/` (`chat.py`,
`ask.py`, `reminder.py`) holding its payload type (stored as
`queue_items.payload`, with the kind's `tag` in `queue_items.kind`) and its
`TurnKind` (`turns/kinds/base.py`). A kind only decides, purely:

- `turn`: the kernel turn it builds from the queue item;
- `opening`: what is recorded when the turn first starts, and whether a
  pending idle-close marker is told first;
- `framing`: the `Framing` the agent gets (`agent/framing.py`): turn rules
  for the system prompt, inline attachments, the input line after the
  history. The agent never branches on kind;
- `question_title`: what asked, on a question the turn parks with;
- `answer`: the text that ends the exchange for the model's reply, or
  `None` for silence;
- `on_failure`: an `Ending`: `Deliver(text)` (recorded and sent, like a
  reply), `Tell(text)` (a notice, sent, not recorded) or `Silent()`;
- `entries` and `message`: what the exchange records for a text, and what
  the user is sent. A question that ends unanswered is recorded through
  `entries` too (`turns/inputs/endings.py`).

The worker does what the kinds decide, through `turns/exchange.py`'s
`Exchange` (the only place that records and sends for a kind). The
delivery rule: the exchange is recorded and sent **before** the queue item
finishes, so a crash repeats it rather than losing it; a `Tell` is sent
**after**, so a crash loses it rather than repeating it.

`Kinds` (`turns/kinds/__init__.py`) is the registry, built once in `app`:
by tag to decode an item, by turn type for everything after. Nothing
outside `turns/kinds/` branches on kind, except toolsets choosing what to
offer (see "Adding a tool group"). The set is closed; adding a kind takes:

- a turn data class in `kernel/turn.py`, added to `TurnInput`;
- a module in `turns/kinds/` with its payload and `TurnKind`, listed in
  `_KIND_TYPES` and `Kinds.default`;
- whatever queues it (like `turns/intake.py` or `turns/due_reminders.py`),
  calling `Queue.add` with its payload;
- the toolsets' decisions about offering their tools on it, and its tests
  in `tests/turns/kinds/`.

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

### Packages and ranks

Every package under `src/alfred/` has a rank, and a module imports only from
its own package or from the rows below it. `a | b` on one row are siblings
that must not import each other; `a : b` may. Imports under `TYPE_CHECKING`
count. `lint-imports` (run by `make lint`) checks this against the contract
in `pyproject.toml`, which copies this table; a new top-level package or
module must be added there.

```
 rank  name       packages / modules
 ----  ---------  ---------------------------------------------------------
  6    entry      app : cli : config : schema : startup
  5    adapters   telegram | http
  4    runtime    turns
                  agent
  3    features   archive | facts | reminders | web | mcp | toolbox
                  conversations
                  attachments
                  brains
  2    kernel     kernel
  1    infra      db | llm | backoff | times
                  settings
```

The feature rows are ordered by the dependencies that are real, not by
taste: `archive` and `facts` read conversation transcripts, `conversations`
renders attachment handles, and `attachments` asks whether the current
brain has vision. A feature may use the features below it, never above.
When a lower package needs something from a higher one, it declares a
protocol and `app` wires in the implementation (the attachment tools'
conversation scope, the input requests' question editor, the recalled
conversations' titles).

A feature package holds everything about its feature, one job per module:

- **domain** types and rules (`reminders/when.py`);
- its **tables** (`tables.py`; see "Adding a table");
- the **store**, its persistence (`facts/store.py`, `conversations/store.py`);
- the **tools** the model calls (`tools.py`, a `LocalToolset`);
- background **jobs** run by `turns/background.py` (`archive/summary_job.py`);
- its **config** section (`config.py`, built on `settings.Section`). Only
  `alfred/config.py` composes the sections into `Config`, and nothing below
  the entry rank imports it: `app` hands each part the section or values it
  needs. A TOML table that feeds several packages (`[ai]`, `[memory]`) is
  read through one class that extends each package's section;
- its fixed user-facing text (`notices.py`).

Tests mirror the packages: the tests of `alfred/facts/store.py` are in
`tests/facts/test_store.py`.

## Adding a table

A table belongs to one feature (or to `turns`) and is defined in that
package's `tables.py`, on `alfred.db.base.Base`; a status vocabulary is a
check constraint built with `in_values`, next to the table. Import the
module in `alfred/schema.py`, which gathers every table for Alembic (and
add a column that names a user to `USER_NAME_COLUMNS`). Then add a
migration under `migrations/versions/` (autogenerate from
`schema.metadata`, then edit by hand); `test_models_match_migrations`
fails until the two agree. Constraint tests go in
`tests/<package>/test_tables.py`.

Three rules keep the data shaped like the code:

1. **Writes.** A package inserts, updates and deletes only its own tables.
2. **Reads.** A package may read, and join, the tables of packages ranked
   below it (`archive/search.py` joins `conversations`), never above. A
   read from above goes through a protocol `app` wires in
   (`conversations/transcript.py`'s `RecalledLookup`).
3. **Foreign keys** point within the package or down the order. A link
   that would point up is a plain column with no foreign key
   (`entries.turn_id`) or a table owned by the higher package
   (`entry_attachments`, `conversation_summaries`).

A write that spans packages runs in one transaction opened by the package
ranked above all of them, which calls each package's session-taking
operation: `turns/intake.py` queues a message with `attachments/rows.py`'s
`add` and `Queue.add`; `turns/due_reminders.py` queues what
`Reminders.claim_due` claims. Such an operation takes the caller's
`AsyncSession` and never commits; the caller notifies anyone waiting
(`Queue.notify`) after its transaction commits.

## Notices

A notice is fixed text alfred sends a user without the model. It lives in
the `notices.py` of the package that sends it. Every notice speaks in the
butler's register and ends its first clause with the user's form of address
(`User.address`), used exactly as configured; notices are not configurable,
even with a custom persona.

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
