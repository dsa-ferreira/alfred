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
  over ("thanks, we're done") starts a fresh one, marked in the chat with
  `— new conversation —`.
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
| `ai.model` | string | `"gemma4:12b-it-qat"` | Model name sent with every request. |
| `ai.request_timeout_s` | number | `300` | Timeout of one model request, in seconds. Long enough for the AI server to wake and load the model. |
| `ai.max_requests_per_turn` | integer | `8` | Model requests allowed in one turn. A turn that needs more is stopped and the owner is told. |
| `ai.tool_result_max_chars` | integer | `4000` | Tool results are cut to this many characters (with a `[truncated]` note) before they reach the model. |
| `ai.persona` | string | built-in butler | alfred's voice. Replaces only the built-in butler persona; the built-in operating rules (tools, service notices, `/ask`) are always sent after it. Sent as written. |
| `ai.owner_address` | string | `"sir"` | How alfred addresses you, at most once per reply. Also ends the fixed notices (failures, text only). Used as written. |
| `history.max_entries` | integer | `30` | Most recent entries of the current conversation (owner messages, replies, notices) sent to the model. |
| `queue.retry_initial_s` | number | `30` | First retry delay while the AI is unreachable, in seconds. Doubles on each attempt. |
| `queue.retry_max_s` | number | `900` | Longest retry delay, in seconds. Retries never give up. |
| `mcp.list_timeout_s` | number | `10` | Time allowed for each MCP server's tool list at the start of a turn. A server that doesn't answer is unavailable for that turn. |
| `mcp.call_timeout_s` | number | `60` | Time allowed for one MCP tool call. |
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
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Prefix of the tool names the model sees: mcparr's `get_downloads` becomes `mcparr_get_downloads`. Characters outside `a-zA-Z0-9_-` become `_`. A name longer than 64 characters, or one that collides with another tool's, is skipped with a warning. |
| `url` | yes | The server's Streamable HTTP endpoint. Servers not configured are never contacted. |
| `token_env` | yes | `ALFRED_*` variable whose value alfred sends as `Authorization: Bearer <token>` on every request to this server. |
| `exclude_tools` | no | Original (unprefixed) tool names. They are not offered to the model, and a call to one is refused without contacting the server. A name the server doesn't have is logged as a warning, which catches typos. |

### Tokens

- **mcparr** — create a token for alfred on mcparr with the scope you want the
  AI to have (read-only, or with the write tools such as `add_media`), and put
  it in the variable named by `token_env`.
- **cookbook** — cookbook keys have no scopes, so every tool is reachable
  with any key. Generate a key for alfred (`cookbook apikey alfred`) and use
  `exclude_tools` to hide the write tools you don't want the AI to use, e.g.
  `exclude_tools = ["delete_recipe"]`.

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

## Deployment

alfred is one container running one process. Run exactly one replica: the
single AI worker and Telegram long polling both assume it. It needs no public
address; it only makes outbound connections (Telegram, the AI server, the MCP
servers, Postgres) and serves the API on port `8080`.

1. **Database.** Create a database and user on the shared Postgres server:

   ```sql
   CREATE USER alfred WITH PASSWORD '<password>';
   CREATE DATABASE alfred OWNER alfred;
   ```

   The container runs `alfred migrate` on every start, before `alfred serve`.

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
