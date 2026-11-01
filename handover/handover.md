# alfred handover

alfred is the owner's personal assistant on Telegram. The owner chats with a bot
from their phone; alfred sends each message to the local AI (`ollama-gpu`),
lets the model use the tools of the homelab MCP servers (`mcparr`, `cookbook`),
and replies in the chat. It also has a small HTTP API that homelab services can
call later to message the owner (`/notify`) or ask the AI whether something is
worth telling them (`/ask`). No service calls it yet.

```yaml
handover: 1
service:
  name: alfred
  summary: Telegram bot and service API in front of the local AI, with its own MCP agent loop
  source: https://github.com/dsa-ferreira/alfred
  release: 0.1.0

placement:
  preference: any
  reason: >
    One small Python process with no volumes. Needs outbound internet
    (api.telegram.org) and network access to ollama-gpu, mcparr, cookbook and
    its Postgres. Must run as exactly ONE replica (single AI worker, and
    Telegram long polling allows only one poller per bot).

exposure:
  hostname: alfred.lan
  tailnet: false                   # the owner reaches alfred through Telegram, not this hostname
  who_calls_it: [homelab services calling /notify and /ask (none yet), healthchecks]

components:
  - name: alfred
    image: album.lan/alfred:0.1.0  # pushed 2026-11-01, linux/amd64
    kind: service
    # Image entrypoint runs `alfred migrate` (alembic upgrade head), then
    # execs CMD `alfred serve`. No command override needed.
    user: "10001:10001"
    ports:
      - port: 8080
        protocol: http
        purpose: service API (/notify, /ask) and /healthz
        public: true
    env:
      ALFRED_DATABASE_URL: {from: dependency, name: db, field: url}
      ALFRED_TELEGRAM_BOT_TOKEN: {from: secret, name: telegram_bot_token}
      ALFRED_MCP_TOKEN_MCPARR: {from: secret, name: mcparr_token}
      ALFRED_MCP_TOKEN_COOKBOOK: {from: secret, name: cookbook_api_key}
      # ALFRED_CONFIG is set in the image to /etc/alfred/alfred.toml; leave it.
    files:
      - path: /etc/alfred/alfred.toml
        format: toml
        template: see "Configuration" below (complete file)
        contains_secrets: []       # secrets are env only; the file names their variables
    volumes: []                    # all state is in Postgres
    healthcheck:
      http: {path: /healthz, port: 8080, expect: 200}   # also baked into the image
      start_period: 15s
    resources:
      cpu: TODO                    # not measured; expected well under 0.5 core (mostly waiting on the AI)
      memory: TODO                 # not measured; expected ~150-250 MB
      gpu: none
    depends_on: [db]

dependencies:
  - name: db
    type: postgres
    version: "17"                  # tested on 17; no extensions used
    needs:
      extensions: []
      databases: [alfred]
      privileges: owner of its database (creates its own tables via migrations); no superuser
    size: a few MB at start; grows slowly with chat volume (text rows only)
    backup: nice_to_have
    shareable: true                # only needs its own database and user
    clients_reach_directly: false
    fields:
      url: postgresql://alfred:<password>@<host>:5432/alfred   # postgres:// also accepted

secrets:
  - name: telegram_bot_token
    generate: user_provided        # from @BotFather (/newbot)
    used_by: [alfred]
    rotation: revoke in BotFather, update the secret, restart alfred. Old token stops working immediately.
  - name: mcparr_token
    generate: hex32
    used_by: [alfred, mcparr]      # mcparr needs the same value in MCPARR_CLIENTS (see Integrations)
    rotation: change on both sides together; until then mcparr's tools are unavailable to alfred (it keeps working without them).
  - name: cookbook_api_key
    generate: hex32
    used_by: [alfred, cookbook]    # cookbook stores only the SHA-256 hex digest of it (see Integrations)
    rotation: change on both sides together; until then cookbook's tools are unavailable to alfred.

integrations:
  - name: ollama-gpu
    direction: uses
    detail: >
      OpenAI-compatible API: POST {base_url}/v1/chat/completions, non-streaming,
      with tools. Model `gemma4:12b-it-qat` must be available on it. No auth.
      Configured as `ai.base_url` in alfred.toml (plain http, no /v1 suffix).
    tolerates_cold_start: true     # 300 s request timeout; messages wait in a queue and are retried while it's down
  - name: mcparr
    direction: uses
    detail: >
      MCP over Streamable HTTP at <mcparr base>/mcp with `Authorization: Bearer <mcparr_token>`.
      Add a client entry `alfred:<mcparr_token>:rw` to mcparr's MCPARR_CLIENTS.
    tolerates_cold_start: true
  - name: cookbook
    direction: uses
    detail: >
      MCP over Streamable HTTP at <cookbook base>/mcp with `Authorization: Bearer <cookbook_api_key>`.
      Add `"alfred": "<sha256 hex of cookbook_api_key>"` to cookbook's COOKBOOK_API_KEYS JSON object.
    tolerates_cold_start: true
  - name: telegram
    direction: uses
    detail: >
      External: outbound HTTPS to api.telegram.org (Bot API long polling and
      sendMessage). No inbound connection, no webhook, no public URL.
    tolerates_cold_start: false

lifecycle:
  migrations: on_start             # entrypoint runs `alfred migrate` before serving
  first_run: >
    1) User creates the bot with @BotFather and stores the token as
    telegram_bot_token. 2) User provides their numeric Telegram user ID for
    telegram.owner_user_id. 3) Create the alfred database and user.
    4) Add alfred's client to mcparr and cookbook, restart them. 5) Start
    alfred. 6) User opens a private chat with the bot and presses Start.
  upgrade: pull the new tag and restart; migrations run on start. Read the update file for config changes.
  restart_safe: true               # SIGTERM shuts down cleanly; an interrupted AI turn runs again on next start

verify:
  - "curl -fsS https://alfred.lan/healthz  ->  {\"status\":\"ok\"}"
  - "curl -s -o /dev/null -w '%{http_code}' -X POST https://alfred.lan/notify -H 'Content-Type: application/json' -d '{\"text\":\"x\"}'  ->  401 (no service token configured yet)"
  - "docker logs alfred  ->  'alfred started: API on 0.0.0.0:8080, model gemma4:12b-it-qat at <ollama url>, 2 MCP server(s)' and no 'MCP server ... is unavailable' warnings after the first message"
  - "Owner sends the bot 'hello' in Telegram  ->  a short reply (first one may take a minute while ollama-gpu wakes)"
  - "Owner sends 'what's downloading?'  ->  an answer with real qBittorrent/Sonarr/Radarr data; logs show 'tool call #1 mcparr_get_downloads ... -> ok'"
```

## Configuration

Non-secret settings come only from the TOML file at `/etc/alfred/alfred.toml`
(path from `ALFRED_CONFIG`, already set in the image). Secrets come only from
environment variables; the file names them with `token_env`, and every
`token_env` must start with `ALFRED_`. There's no precedence between the two:
each setting has one source. Everything is validated at startup. Unknown
keys, wrong types, bad URLs, or a missing or empty variable all make alfred
exit with a list of every problem found. Error messages never include
secret values.

Render this file. Only `owner_user_id` and the three URLs need filling in.
Everything else shown is the default and can be left out.

```toml
[telegram]
# The owner's numeric Telegram user ID (user provides). Only this user's
# private chat with the bot is answered; everyone else is ignored.
owner_user_id = 0

[ai]
# ollama-gpu, plain http, no /v1 suffix. alfred posts to {base_url}/v1/chat/completions.
base_url = "http://<ollama-gpu>:11434"
model = "gemma4:12b-it-qat"
request_timeout_s = 300        # covers the GPU box waking up
max_requests_per_turn = 8
tool_result_max_chars = 4000
# system_prompt = """..."""    # optional; replaces the built-in prompt

[history]
max_entries = 30

[queue]
retry_initial_s = 30           # retry delay while the AI is unreachable; doubles...
retry_max_s = 900              # ...up to this. Retries never give up.

[mcp]
list_timeout_s = 10
call_timeout_s = 60

[server]
host = "0.0.0.0"
port = 8080
log_level = "INFO"

# No services call /notify or /ask yet. Each future caller gets an entry
# plus an ALFRED_SERVICE_TOKEN_<NAME> secret (hex32), shared with that service.
# [[services]]
# name = "backup"
# token_env = "ALFRED_SERVICE_TOKEN_BACKUP"

[[mcp_servers]]
name = "mcparr"                # tool prefix; must stay "mcparr"
url = "http://<mcparr backend>/mcp"
token_env = "ALFRED_MCP_TOKEN_MCPARR"
exclude_tools = []

[[mcp_servers]]
name = "cookbook"
url = "http://<cookbook backend>/mcp"
token_env = "ALFRED_MCP_TOKEN_COOKBOOK"
exclude_tools = []             # owner's choice: all cookbook tools allowed
```

Things that aren't obvious:

- **Use plain-HTTP backend URLs for ollama-gpu, mcparr and cookbook, not
  `https://*.lan`.** The image does not trust the homelab CA. The MCP client
  library (httpx2) ignores `SSL_CERT_FILE`, so mounting the CA isn't enough.
  alfred's firewall rules therefore need its host to reach those backends'
  HTTP ports directly.
- `[server] port` must match the published container port and the healthcheck
  (the image's built-in `HEALTHCHECK` assumes 8080).
- Server names (`mcparr`, `cookbook`) prefix the tool names the AI sees, so
  renaming one changes the tools' names. `alfred` is reserved.
- Changing any setting needs a restart; nothing is reloaded live.

## Data and backups

All state is in the `alfred` Postgres database:

- the conversation history, with old conversations archived;
- the queue of messages waiting for the AI, including any still waiting
  because the AI was unreachable;
- an audit log of the tool calls the AI made.

Back up with `pg_dump` of that one database while alfred runs; no need to stop
it. To restore, load the dump into an empty `alfred` database and start
alfred. Losing the database loses the chat history and audit log, but
nothing breaks: alfred recreates the schema on start.

## First run

1. The owner creates the bot: in Telegram, message @BotFather, send `/newbot`,
   pick a name and username, and store the token in the vault as
   `telegram_bot_token`.
2. The owner gets their numeric user ID (e.g. by messaging @userinfobot) for
   `telegram.owner_user_id`. It isn't a secret, but it comes from the owner.
3. Deploy. The first start creates the tables.
4. The owner opens a private chat with the new bot and presses **Start**.
   The bot's first answer can be slow while ollama-gpu wakes.

alfred exits at startup if Telegram rejects the bot token or can't be reached,
and if the configuration is invalid. Use a restart policy
(`restart: unless-stopped`) so a brief Telegram or network outage during boot
doesn't leave it down. Once running, Telegram outages are retried
internally.

## Integrations

- **ollama-gpu.** Each owner message is one "turn": up to
  `max_requests_per_turn` chat-completion requests while the model calls
  tools. Connection errors, timeouts and 5xx answers mean the turn waits in
  the queue and is retried with backoff (30 s doubling to 15 min, forever),
  in order. A turn is never retried once a tool has run, so actions aren't
  repeated. In that case the owner is told the request failed. Changing
  the model or switching to another OpenAI-compatible server is a config
  change only.
- **mcparr** (`rw`). The AI can read downloads, upcoming releases and
  search, and can add movies and series. Needs the new client entry
  `alfred:<mcparr_token>:rw` appended to mcparr's `MCPARR_CLIENTS`, then an
  mcparr restart.
- **cookbook.** Every cookbook tool is offered, including writes
  (`exclude_tools` is empty on purpose). cookbook keeps API keys as SHA-256
  hex digests in `COOKBOOK_API_KEYS`, so add
  `"alfred": "<sha256 hex of cookbook_api_key>"`
  (`printf %s "$KEY" | sha256sum`), then restart cookbook.
- **MCP servers down.** At the start of every turn alfred lists each
  server's tools, waiting at most 10 s. A server that's down, slow, or
  rejects the token is skipped for that turn only and a warning is logged.
  The AI is told it's unavailable, and the next turn tries again. A wrong
  token therefore shows up as `MCP server <name> is unavailable this turn` in
  the logs, not as a startup failure.
- **Services calling alfred.** None yet. Each future caller needs a
  `[[services]]` entry, an `ALFRED_SERVICE_TOKEN_<NAME>` secret (hex32) shared
  with it, and network access to alfred's port. It calls
  `POST /notify {"text": ...}` (200 once delivered to Telegram, 502 if not) or
  `POST /ask {"prompt": ...}` (202) with `Authorization: Bearer <token>`. It
  will come as an update file when the first caller exists.

## Open questions

- `resources`: CPU and memory weren't measured under load. The figures above
  are expectations; size the container small and adjust.

## Not covered

- No services are configured as `/notify`/`/ask` callers yet.
- No access through the tailnet; the owner uses Telegram, which works
  anywhere.
- No metrics endpoint. Logs go to stdout.
