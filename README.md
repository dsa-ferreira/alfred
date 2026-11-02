# alfred

alfred is a Telegram bot that is its owner's two-way channel to the local AI
and the homelab services.

- **Owner → AI.** Every text message the owner sends the bot goes to the AI
  (Ollama on `ai.lan` by default, or any OpenAI-compatible server). alfred runs
  the agent loop itself: it offers the model the tools of the configured MCP
  servers (mcparr, cookbook, ...), runs the tool calls, and sends back the
  final answer, formatted for Telegram.
- **Services → owner.** Homelab services call alfred's HTTP API:
  `POST /notify` sends the owner a message directly, and `POST /ask` lets the
  AI decide whether the owner needs to hear about something.
- **Memory.** One current conversation is kept in Postgres. The AI sees the
  most recent entries, including service notices. Saying the conversation is
  over ("thanks, we're done") archives it with a short title and summary and
  starts a fresh one, marked in the chat with `— #41 "Chicken thighs recipe" —`
  and `— new conversation #42 —`. Later, "remember conversation 41" or
  "remember when we talked about the chicken recipe last week?" makes the AI
  search the archived conversations (by meaning through the embedding service
  on `ragman.lan`, by keywords, and by date) and bring one back.
- **Outages.** Messages for the AI go through one persisted queue. While the
  AI is unreachable they wait and are retried in order; nothing is lost.

## Architecture

```
                 long polling                       OpenAI-compatible
  Telegram  <─────────────────┐                   ┌───────────────────>  AI (ai.lan)
  (owner)                     │                   │   /v1/chat/completions
                       ┌──────┴───────────────────┴──────┐
                       │              alfred             │
                       │  ┌─────────┐   ┌─────────────┐  │   Streamable HTTP
                       │  │ aiogram │──>│    queue    │──┼─────────────────>  MCP servers
                       │  │   bot   │   │ (1 worker,  │  │   bearer token        (mcparr,
                       │  └─────────┘   │  agent loop)│  │                        cookbook)
   services  ──────────┼> FastAPI ─────>└─────────────┘  │
   /notify, /ask       │  /notify ──> Telegram directly   │
                       └───────────────┬─────────────────┘
                                       │
                                   Postgres
                     (conversations, entries, queue, tool calls)
```

One process, one event loop: the bot, the API and the queue worker run as
three asyncio tasks. Only one AI turn runs at a time.

## Configuration

Non-secret settings live in one TOML file, `alfred.toml` in the working
directory or the path in `ALFRED_CONFIG`. Start from
[`alfred.toml.example`](alfred.toml.example), which lists every key with its
default. Secrets are only read from `ALFRED_*` environment variables.

Everything is checked once at startup. Unknown keys, wrong types, invalid
URLs, duplicate names, duplicate service tokens and missing variables are all
reported together and alfred exits. Error messages never contain secret values.

### `alfred.toml`

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `telegram.owner_user_id` | integer | required | Your Telegram user ID. Only this user's private chat with the bot is answered; notices and `/ask` answers go to this user. |
| `ai.base_url` | URL | `"http://ai.lan"` | OpenAI-compatible server. alfred posts to `{base_url}/v1/chat/completions`. |
| `ai.model` | string | `"gemma4:26b-a4b-it-qat"` | Model name sent with every request. |
| `ai.request_timeout_s` | number | `300` | Timeout of one model request, in seconds. Long enough for the AI server to wake and load the model. |
| `ai.max_requests_per_turn` | integer | `8` | Model requests allowed in one turn. A turn that needs more is stopped and the owner is told. |
| `ai.tool_result_max_chars` | integer | `4000` | Tool results are cut to this many characters (with a `[truncated]` note) before they reach the model. |
| `ai.persona` | string | built-in butler | alfred's voice. Replaces only the built-in butler persona; the built-in operating rules (tools, service notices, `/ask`) are always sent after it. Sent as written. |
| `ai.owner_address` | string | `"sir"` | How alfred addresses you, at most once per reply. Also ends the fixed notices (failures, text only). Used as written. |
| `ai.timezone` | string | `"Europe/Lisbon"` | Your IANA timezone. Today's date in the system prompt and the dates of conversation search use it. An unknown name stops alfred at startup. |
| `history.max_entries` | integer | `30` | Most recent entries of the current conversation (owner messages, replies, notices, recalled conversations) sent to the model. Only the newest recall is sent in full; older ones as their summary. |
| `queue.retry_initial_s` | number | `30` | First retry delay while the AI is unreachable, in seconds. Doubles on each attempt. |
| `queue.retry_max_s` | number | `900` | Longest retry delay, in seconds. Retries never give up. |
| `memory.summary_model` | string | `ai.model` | Model that writes the title and summary of an ended conversation, on `ai.base_url`. |
| `memory.embedding_base_url` | URL | `"http://ragman.lan"` | OpenAI-compatible embedding server. alfred posts to `{embedding_base_url}/v1/embeddings`. |
| `memory.embedding_model` | string | `"nomic-embed-text:v1.5"` | Embedding model. Changing it re-embeds the archived conversations in the background. |
| `memory.embedding_timeout_s` | number | `30` | Timeout of one embedding request, in seconds. |
| `memory.document_prefix` | string | `"search_document: "` | Put before a conversation's title and summary when embedding them. Model-specific (these are nomic's). |
| `memory.query_prefix` | string | `"search_query: "` | Put before a search query when embedding it. Model-specific. |
| `memory.recall_max_chars` | integer | `32000` | Largest recalled conversation, in characters. A longer one is recalled as its summary plus the end of its transcript. |
| `memory.summary_input_max_chars` | integer | `60000` | Most transcript characters sent to the summary model; a longer transcript keeps its start and end. |
| `memory.search_results` | integer | `5` | Most conversations a search returns. |
| `mcp.list_timeout_s` | number | `10` | Time allowed for each MCP server's tool list at the start of a turn. A server that doesn't answer is unavailable for that turn. |
| `mcp.call_timeout_s` | number | `60` | Time allowed for one MCP tool call. |
| `mcp.instructions_max_chars` | integer | `2000` | Each MCP server's instructions are cut to this many characters (with a `[truncated]` note and a logged warning) before they reach the model. |
| `server.host` | string | `"0.0.0.0"` | Listen address of the service API. |
| `server.port` | integer | `8080` | Listen port of the service API. |
| `server.log_level` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` | `"INFO"` | Log level. |
| `[[services]]` | list | none | Services allowed to call `/notify` and `/ask`. |
| `services.name` | name | required | Service name. Shown to the owner as `[name]` and to the AI as the sender. |
| `services.token_env` | variable name | required | `ALFRED_*` variable holding the service's bearer token. Tokens must be unique. |
| `[[mcp_servers]]` | list | none | MCP servers whose tools the model is offered (see [MCP servers](#mcp-servers)). |
| `mcp_servers.name` | name | required | Server name, used as the tool prefix (`<name>_<tool>`). `alfred` is reserved. |
| `mcp_servers.url` | URL | required | Streamable HTTP endpoint, e.g. `http://mcparr.lan:8090/mcp`. |
| `mcp_servers.token_env` | variable name | required | `ALFRED_*` variable holding the bearer token alfred sends to this server. |
| `mcp_servers.exclude_tools` | list of strings | `[]` | Original (unprefixed) tool names hidden from the model and refused if called. |
| `mcp_servers.use_server_instructions` | boolean | `true` | Give the model the instructions the server sends when connecting. `false` mutes them; the tools are still offered. |

Names are lowercase letters, digits and hyphens. Variable names must start
with `ALFRED_` and use `A-Z`, `0-9` and `_`.

### Environment variables

| Variable | Required | Description |
| --- | --- | --- |
| `ALFRED_CONFIG` | no | Path of the TOML file. Default `alfred.toml` in the working directory. |
| `ALFRED_TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather. |
| `ALFRED_DATABASE_URL` | yes | Postgres URL, e.g. `postgresql://alfred:<password>@postgres.lan/alfred`. `postgres://` and `postgresql://` are switched to the psycopg 3 driver. `alfred migrate` needs only this one. |
| the `token_env` of each service | yes | Bearer token the service sends to `/notify` and `/ask`. |
| the `token_env` of each MCP server | yes | Bearer token alfred sends to that MCP server. |

## Service API

Services authenticate with `Authorization: Bearer <token>`, where the token is
the value of a `[[services]]` entry's `token_env` variable. The matching
service's name is the sender. A missing, empty or unknown token gets
`401` and nothing else happens. The API listens on `server.host:server.port`
(default `0.0.0.0:8080`).

### `POST /notify`

Sends the owner a Telegram message directly, prefixed with the service name.
The AI is not involved and the message doesn't wait behind queued AI work.
Once delivered, it is recorded in the conversation as a notice from the
service, so the owner can ask the AI about it.

```sh
curl -sS -X POST http://alfred.lan:8080/notify \
  -H "Authorization: Bearer $ALFRED_SERVICE_TOKEN_BACKUP" \
  -H "Content-Type: application/json" \
  -d '{"text": "Nightly backup failed: disk full"}'
```

The owner sees `[backup] Nightly backup failed: disk full`.

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{"status": "sent"}` | Telegram accepted the message. |
| `401` | `{"detail": "missing or invalid bearer token"}` | Bad token. |
| `422` | validation details | `text` missing, empty or not a string. |
| `502` | `{"detail": "Telegram did not accept the message; retry later"}` | Delivery failed. Nothing was recorded; retry later. |

### `POST /ask`

Queues an AI turn on behalf of the service and returns at once. The AI sees
the recent conversation and the prompt, and decides whether the owner needs
to know. If it answers exactly `NO_REPLY`, nothing is sent (the run is only
logged). Otherwise the answer is sent to the owner prefixed with the service
name, and the prompt and answer become part of the conversation.

```sh
curl -sS -X POST http://alfred.lan:8080/ask \
  -H "Authorization: Bearer $ALFRED_SERVICE_TOKEN_DISK_CHECK" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "/mnt/data is at 93%, was 80% yesterday"}'
```

| Status | Body | Meaning |
| --- | --- | --- |
| `202` | `{"id": 17}` | Queued; `id` is the queue item, as seen in alfred's logs. |
| `401` | `{"detail": "missing or invalid bearer token"}` | Bad token. |
| `422` | validation details | `prompt` missing, empty or not a string. |

`/ask` items share the queue with the owner's messages: they run one at a
time, in arrival order, and wait while the AI is unreachable.

### `GET /healthz`

No authentication. Returns `200 {"status": "ok"}` while the process is
running; it contacts nothing (not Telegram, the AI or MCP servers).

```sh
curl -sS http://alfred.lan:8080/healthz
```

## MCP servers

alfred is an MCP client. Each `[[mcp_servers]]` entry is one Streamable HTTP
server whose tools the model is offered:

```toml
[[mcp_servers]]
name = "mcparr"                        # tool prefix; lowercase, digits, hyphens; not "alfred"
url = "http://mcparr.lan:8090/mcp"     # Streamable HTTP endpoint
token_env = "ALFRED_MCP_TOKEN_MCPARR"  # variable holding the bearer token
exclude_tools = []                     # original tool names to hide (default: none)
use_server_instructions = true         # pass the server's instructions to the model
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Prefix of the tool names the model sees: mcparr's `get_downloads` becomes `mcparr_get_downloads`. Characters outside `a-zA-Z0-9_-` become `_`. A name longer than 64 characters, or one that collides with another tool's, is skipped with a warning. |
| `url` | yes | The server's Streamable HTTP endpoint. Servers not configured are never contacted. |
| `token_env` | yes | `ALFRED_*` variable whose value alfred sends as `Authorization: Bearer <token>` on every request to this server. |
| `exclude_tools` | no | Original (unprefixed) tool names. They are not offered to the model, and a call to one is refused without contacting the server. A name the server doesn't have is logged as a warning, which catches typos. |
| `use_server_instructions` | no | Default `true`. Whether the instructions the server sends when connecting reach the model (see [Server instructions](#server-instructions)). `false` mutes them; the server's tools are still offered. |

### Tokens

- **mcparr** — create a token for alfred on mcparr with the scope you want the
  AI to have (read-only, or with the write tools such as `add_media`), and put
  it in the variable named by `token_env`.
- **cookbook** — cookbook keys have no scopes, so every tool is reachable
  with any key. Generate a key for alfred (`cookbook apikey alfred`) and use
  `exclude_tools` to hide the write tools you don't want the AI to use, e.g.
  `exclude_tools = ["delete_recipe"]`.

### Server instructions

An MCP server can send instructions when alfred connects: how its tools fit
together and when to use them. Every turn, alfred adds each available server's
instructions to the system prompt after the operating rules, one block per
server, telling the model that the server's tools are offered as
`<server>_<tool>` (the text itself names them unprefixed). A server that is
unavailable for the turn, sends no instructions, or has
`use_server_instructions = false` adds nothing. Each server's text is cut to
`mcp.instructions_max_chars`, with a warning in the log naming the server.
Instructions are read on the same connection as the tool list, so a server's
new text is used from the next turn.

### Unavailable servers

At the start of every turn alfred lists each server's tools, all servers at
once, waiting at most `mcp.list_timeout_s`. A server that can't be reached,
answers with an error, rejects the token, or is too slow is left out of that
turn only: the model gets the other servers' tools and is told which servers
are unavailable. The next turn tries again, so a server that comes back is
used without restarting alfred.

Tool calls wait at most `mcp.call_timeout_s`. Every failure (unknown or
excluded tool, invalid arguments, server error, timeout) is given to the model
as the tool's result so it can recover or explain; it never ends the turn.
Results are cut to `ai.tool_result_max_chars`.

Local tools use the reserved `alfred_` prefix (for example
`alfred_end_conversation`) and are handled by alfred itself, which is why no
MCP server may be named `alfred`.

### Context size

Every server's tools are sent to the model with every request, so each server
you add makes every request bigger. To see by how much, each turn logs one
line at INFO with the size in characters of what it sends. The line covers the
system prompt (and each server's instructions inside it), the tool definitions
in total, per server and for alfred's own tools, and the history (with the
`/ask` input):

```
turn 812 context: system 5214 chars (instructions: cookbook 640); tools 15 defs 28410 chars (mcparr 6204, cookbook 19276, alfred 2930); history 30 entries 11032 chars
```

After every model request, alfred logs the token counts the model server
reported. Conversation summaries get the same line:

```
turn 812 request 1: 6812 prompt tokens, 38 completion tokens
turn 812 request 2: 7240 prompt tokens, 112 completion tokens
conversation 41 summary: 3120 prompt tokens, 58 completion tokens
```

A server that sends no counts gets `token counts not reported` instead. Some
servers only count the prompt tokens they had to process anew when they reuse
a cached prompt. Ollama may be one of them. With such a server, a later request
in a turn can show fewer prompt tokens than the one before it. The character
sizes above don't depend on the server.

With `server.log_level = "DEBUG"`, each turn also lists its five largest tool
definitions, largest first, to find a single oversized schema:

```
turn 812 largest tools: cookbook_update_recipe 4120, cookbook_create_recipe 3980, mcparr_add_media 1210, ...
```

## Deployment

alfred is one container running one process. Run exactly one replica: the
single AI worker and Telegram long polling both assume it. It needs no public
address; it only makes outbound connections (Telegram, the AI server, the
embedding server, the MCP servers, Postgres) and serves the API on port `8080`.

1. **Database.** Create a database and user on the shared Postgres server:

   The server needs the [pgvector](https://github.com/pgvector/pgvector)
   extension installed (for example the `pgvector/pgvector:0.8.7-pg17-trixie`
   image). Creating it needs a superuser, so do it here, not in alfred:

   ```sql
   CREATE USER alfred WITH PASSWORD '<password>';
   CREATE DATABASE alfred OWNER alfred;
   \c alfred
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

   The container runs `alfred migrate` on every start, before `alfred serve`.
   It stops with a message saying so if the extension is missing.

2. **Telegram bot.** In Telegram, talk to [@BotFather](https://t.me/BotFather),
   send `/newbot`, and keep the token it gives you
   (`ALFRED_TELEGRAM_BOT_TOKEN`). Find your own user ID, for example by
   messaging [@userinfobot](https://t.me/userinfobot), and put it in
   `telegram.owner_user_id`. Then open a private chat with your bot and
   press Start. Don't add the bot to groups; alfred ignores them.

3. **MCP tokens.** Create a token for alfred on each MCP server (see
   [MCP servers](#mcp-servers)): an mcparr token with the scope you choose, and
   a cookbook key (`cookbook apikey alfred`) plus `exclude_tools` for the
   cookbook tools alfred shouldn't use.

4. **Service tokens.** For each service that will call `/notify` or `/ask`,
   generate a random token (e.g. `openssl rand -base64 32`), add a
   `[[services]]` entry, and give the same token to the service.

5. **Config file.** Write `alfred.toml` from
   [`alfred.toml.example`](alfred.toml.example) and mount it read-only at
   `/etc/alfred/alfred.toml` (the image sets `ALFRED_CONFIG` to that path).

6. **Secrets.** Set the environment variables:

   | Variable | Value |
   | --- | --- |
   | `ALFRED_TELEGRAM_BOT_TOKEN` | the BotFather token |
   | `ALFRED_DATABASE_URL` | `postgresql://alfred:<password>@<postgres host>/alfred` |
   | each MCP server's `token_env` | e.g. `ALFRED_MCP_TOKEN_MCPARR`, `ALFRED_MCP_TOKEN_COOKBOOK` |
   | each service's `token_env` | e.g. `ALFRED_SERVICE_TOKEN_BACKUP` |

7. **Run** the image (`make image` builds `alfred:<version>` and
   `alfred:latest`; `make push` pushes both to `album.lan`):

   ```sh
   docker run -d --name alfred --restart unless-stopped \
     -p 8080:8080 \
     -v /srv/alfred/alfred.toml:/etc/alfred/alfred.toml:ro \
     --env-file /srv/alfred/alfred.env \
     album.lan/alfred:latest
   ```

   or with Compose:

   ```yaml
   services:
     alfred:
       image: album.lan/alfred:latest
       restart: unless-stopped
       ports:
         - "8080:8080"
       volumes:
         - ./alfred.toml:/etc/alfred/alfred.toml:ro
       env_file: alfred.env   # the ALFRED_* secrets; keep out of git
   ```

At startup alfred validates the whole configuration and exits with a list of
problems if anything is wrong. It also exits if Telegram rejects the bot
token, so a restart policy is needed to ride out a Telegram outage during
start-up. The image's `HEALTHCHECK` calls `/healthz`. Stopping the container
(SIGTERM) shuts down cleanly; a turn in progress is interrupted and runs again
on the next start.
