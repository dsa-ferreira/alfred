# Design

## Context

- `conversations` has `id`, `started_at` and `ended_at`. At most one row has
  `ended_at IS NULL` (the current conversation). `entries` has a unique index
  on `(queue_item_id, kind)` so that a retried turn records its entries once.
- A conversation only ends through `alfred_end_conversation`. The worker's
  `_complete()` (`src/alfred/queue.py`) sends the reply, calls
  `History.reset()` and sends the fixed marker `— new conversation —`.
- The worker is serial and head-of-line: an item that keeps failing because
  the AI is unreachable blocks everything behind it.
- `alfred_end_conversation` is a local tool. `Agent.run` inserts it in owner
  turns and handles it in `_run_tool` before calling MCP.
- `History.recent()` returns the newest N entries; `to_messages()` maps them
  to chat messages, prefixing notices and asks (`[notice:<service>]`).
- Chat model: `gemma4:12b-it-qat` on `ai.lan`, which may be asleep. Its
  context is large (the infra repo sets 262144; the owner budgets for 32k).
  Embeddings: Ollama on `ragman.lan`, CPU-only, always loaded, serving
  `nomic-embed-text:v1.5` (768 dimensions, 2048-token input). It is reachable
  over plain HTTP through nginx, and its write endpoints are blocked there.
- Postgres 17, plain image, no extensions. alfred's role owns its database
  but is not a superuser.

## Goals / Non-Goals

**Goals:**
- One extra model request per ended conversation, no extra requests per turn
  except when the model searches.
- Search never wakes brainiac (embedding the query goes to ragman).
- Memory failures never block the owner's messages.

**Non-Goals:**
- Embedding chunks of transcripts. One vector per conversation, plus keyword
  search over entries, is the first step.
- Automatic recall (alfred bringing up old conversations unasked).
- Editing or deleting summaries, or resuming an archived conversation as the
  current one.
- Recall in `/ask` turns.
- The time of day in the prompt (D12).

## Decisions

### D1. Storage: pgvector, one vector per conversation

New nullable columns on `conversations`:
- `title text`, `summary text`, `summarised_at timestamptz`
- `embedding vector` (no fixed dimension) and `embedding_model text`
- `summary_attempts int default 0`, `summary_next_at timestamptz`
- `search_tsv tsvector`, generated from `title || ' ' || summary` with the
  `english` configuration, with a GIN index.

`entries` gets a GIN expression index on `to_tsvector('english', text)`.

The embedding column has no fixed dimension, and each row records the model
that made it. Search compares only rows whose `embedding_model` equals the
configured model; the sweeper (D4) re-embeds rows made with another model. So
changing `embedding_model` in config needs no migration. The cost is that a
dimensionless column can't have an HNSW index, so search scans every row.
With hundreds or a few thousand conversations, that is milliseconds.

*Alternatives:* `real[]` with cosine in Python (no extension, but every
search loads every vector into memory and the query logic lives in Python);
`vector(768)` with HNSW (faster at a scale alfred won't reach, and a model
change would need a migration). The owner accepted an extension.

### D2. The extension is created outside alfred's migrations

`CREATE EXTENSION vector` needs a superuser (pgvector is not a trusted
extension). The deploy's database init script creates it. Migration `0002`
checks `pg_extension` first and, if `vector` is missing, stops with a clear
error saying to create it as a superuser. It never tries to create it itself.
The test fixture uses a pgvector Postgres image and creates the extension as
the container's superuser before migrating.

The Python side uses the `pgvector` package's SQLAlchemy `Vector` type
(psycopg 3 is supported).

### D3. Reset sequence: archive, then summarise inline, then marker

In `_complete()`, after the reply is sent:

```
archived_id, new_id = history.reset()       # notices from now on go to new_id
title = memory.summarise(archived_id)       # model + embedding; None on any failure
send(marker(archived_id, title, new_id))
```

Archiving first means a notice that arrives while the summary is being made
already lands in the new conversation. `reset()` returns both ids.

`summarise()` makes at most one attempt inline, with `ai.request_timeout_s`
for the model and `memory.embedding_timeout_s` for ragman. Brainiac was just
used for the reply, so it is awake, and the wait is usually a few seconds.
Any failure is logged and the conversation is left to the sweeper. If the
summary succeeded but the embedding failed, the title is still known, so the
marker shows it, and the sweeper only redoes the embedding.

Marker, one message:

```
— #41 "Chicken thighs recipe" —
— new conversation #42 —
```

or, without a title, `— #41 archived —` on the first line.

### D4. Background retries: a sweeper task, not queue items

A fourth asyncio task in `serve` finds archived conversations with no summary,
or no embedding for the configured model, whose `summary_next_at` is due. It
processes them one at a time, oldest first. A failure increments
`summary_attempts` and sets `summary_next_at` with the queue's backoff
settings (`queue.retry_initial_s` doubling to `queue.retry_max_s`). The task
sleeps until the next due row, or for `retry_max_s` when there is none. The
worker nudges it after each reset.

*Alternative:* a `summarise` queue item kind, as first discussed. Rejected:
the queue is head-of-line, so a summary stuck retrying because ragman is down
would hold up every owner message behind it, which the spec forbids. The
sweeper also handles conversations from before the upgrade (backfill) and
re-embedding after a model change, with no extra code path.

The sweeper's model requests can overlap with a turn's. Ollama on brainiac
serves one request at a time (`ollama_num_parallel: 1`), so the turn waits a
few seconds at worst. That is acceptable for a rare background job.

### D5. Summary prompt and parsing

A fixed system prompt asks for exactly two lines, `Title: …` (at most about
six words) and `Summary: …` (one or two sentences, naming concrete outcomes
such as names, numbers and decisions). The transcript follows as one user
message, rendered as `Owner:`, `alfred:`, `[notice:<service>]`,
`[ask:<service>]`, and recalls as `[recalled #<id> "<title>"]` (their title
only, not their transcript, so a recall doesn't get summarised into the new
conversation).

Parsing is lenient: it accepts any case, extra whitespace, quotes and
Markdown bold. If there is no `Title:` line, the first five words of the
summary become the title. A reply with neither line counts as a failure.

*Alternative:* `response_format: json_object`. Ollama supports it, but that
ties alfred to servers that support it. Two labelled lines are robust enough
with a 12B model and keep alfred plain OpenAI-compatible.

If the transcript is longer than `summary_input_max_chars`, the first and last
halves of the budget are kept with `[… N characters omitted …]` in between.
The start of a conversation usually says what it is about, and the end says
how it turned out.

The model is `memory.summary_model`, or `ai.model` if unset, on `ai.base_url`.
A second `ModelClient` is created only if the model differs.

### D6. Embedding client

`POST {memory.embedding_base_url}/v1/embeddings` with
`{"model": ..., "input": ...}` using `httpx`, the same way `ModelClient`
calls chat completions. Stored text is
`memory.document_prefix + title + ". " + summary`. The query is
`memory.query_prefix + query`. The prefixes default to nomic's
`search_document: ` and `search_query: `, and are configurable because they
are model-specific.

### D7. Search: vector and keyword results combined by rank, with a date filter

`alfred_search_conversations(query?, after?, before?)`. All three are
optional, but at least a query or one date is required; otherwise the tool
returns an error result.

The date filter is applied first, inside each candidate query below. `after`
and `before` are local dates in `ai.timezone`, turned into the instants
`after 00:00` and `before + 1 day 00:00` in that zone. A conversation matches
when `started_at < upper` and `ended_at >= lower`, so its span overlaps the
range. An unparseable date is an error result naming the argument.

With a query:

1. Embed the query. On failure, skip step 2 and note it in the result.
2. Vector candidates: archived conversations with an embedding for the
   configured model, ordered by `embedding <=> :q`, top `search_results * 2`.
3. Keyword candidates: archived conversations where
   `search_tsv @@ websearch_to_tsquery('english', :q)` or any of their entries
   matches, ranked by `ts_rank`, top `search_results * 2`.
4. Merge with reciprocal rank fusion (`sum(1 / (60 + rank))`) and keep
   `search_results`.

Without a query, the conversations in the range are returned newest first
(by `ended_at`), with no match label.

Each result shows `#id`, title (or "(no summary yet)"), start and end dates in
`ai.timezone` (`Tue 3 Nov 2026`), the summary, and, for query searches, a
match strength label from its fused score relative to the best one (`best`,
`close`, `weak`). The labels let the model follow the tool description's
rule: recall a clear winner, ask when two are `close`.

*Alternative:* return raw cosine scores. They are hard for a small model to
compare, and keyword-only matches have no cosine score.

*Alternative for dates:* accept relative phrases ("last week") and parse them
in alfred. Rejected: it means writing a natural-language date parser. The
model converts phrases to dates itself, using the date in the system prompt
(D12).

### D8. Recall entries

`entries` gets a new kind `recall` and a nullable column
`recalled_conversation_id` (FK to `conversations`). A partial unique index on
`(conversation_id, recalled_conversation_id) WHERE kind = 'recall'` enforces
"not twice into the same conversation". Recall entries have a null
`queue_item_id`, so they don't collide with the turn's own entries in the
`(queue_item_id, kind)` index. Retries are not a concern: a turn that ran a
tool is never retried.

`text` holds the rendered recall made at recall time:

```
#41 "Chicken thighs recipe", Sat 3 Oct 2026 to Sat 3 Oct 2026 (dates in ai.timezone)
Owner: ...
alfred: ...
```

The size is capped at `recall_max_chars`. A longer conversation is stored as
its summary, then `[earlier part omitted]`, then the transcript's last part
that fits.

`alfred_recall_conversation(id)` writes the entry into the current
conversation (taking the same current-conversation lock as `append`) and
returns the same text as its result. It refuses an unknown id, the current
conversation and a duplicate, as error results.

### D9. Window rendering

`History.recent()` also returns `recalled_conversation_id` and, through a
join, the recalled conversation's title and summary. `to_messages()` renders
recall entries as `user` messages:

- the newest recall in the window:
  `[recall:#41 "Chicken thighs recipe"] <stored text>`
- older recalls: `[recall:#41 "Chicken thighs recipe"] Summary: <summary>`

A new paragraph in `OPERATING_RULES` explains that `[recall:#<id> …]`
messages are earlier conversations brought back at the owner's request, that
the owner did not just say them, and that the owner can be told which
conversation it was.

### D10. Tool definitions

Both tools are defined next to `END_CONVERSATION_TOOL` and are offered only
in owner turns. `_run_tool` dispatches them to a `Memory` service (summary,
embedding, search and recall), which `Agent` receives in its constructor.
Tool descriptions:

- search: "Search earlier, archived conversations with the owner by topic,
  by date, or both. Use when the owner refers to something discussed before
  ('remember when…', 'that recipe we talked about last week', 'what did we
  talk about on Tuesday?'). Turn relative dates into `after`/`before`
  (YYYY-MM-DD, inclusive) using today's date from the system prompt. If one
  result is clearly the best match, recall it and tell the owner which
  conversation you picked (its title and date). If several match about
  equally, ask the owner which one they meant." Parameters: `query`
  (string), `after` and `before` (string, `YYYY-MM-DD`), none required.
- recall: "Bring an archived conversation, by its number, into this
  conversation so you can use what was said. Use when the owner names a
  conversation number or after search found the one they mean."

Both results count as tool calls (they are recorded in `turn_tool_calls` and
make a retry unsafe), like every other tool.

### D11. Configuration

```toml
[ai]
# ...existing keys...
timezone = "Europe/Lisbon"   # IANA name; validated with zoneinfo at startup

[memory]
# summary_model = "gemma4:12b-it-qat"   # default: [ai] model
embedding_base_url = "http://ragman.lan"
embedding_model = "nomic-embed-text:v1.5"
embedding_timeout_s = 30
document_prefix = "search_document: "
query_prefix = "search_query: "
recall_max_chars = 32000
summary_input_max_chars = 60000
search_results = 5
```

The values shown are the defaults and the recommended values for the
homelab. The handover update repeats them with a short reason for each.

`timezone` sits under `[ai]`, next to `owner_address`, because it is about the
owner and is used outside memory too (the prompt date, D12). The image is
`python:3.13-slim`, so `zoneinfo` must not depend on the system's zone
files: the `tzdata` package becomes an explicit dependency.

### D12. Today's date in the system prompt

`OPERATING_RULES` gains a last line, `Today is Monday, 2 November 2026.`,
computed per turn in `ai.timezone`, for owner and `/ask` turns alike. It
gives the date and weekday only, with no time of day.

Ollama reuses the processed prompt when the start of a request matches the
previous one. The conversation history comes after the system prompt, so any
change to the system prompt makes it reprocess the whole history. A
date-only line changes once a day; with the time of day it would change on
every message. Nothing here needs the time of day.

`system_prompt()` takes the date as an argument (the agent passes
`now(tz).date()`), so tests fix it without patching the clock.

*Alternative:* an `alfred_current_date` tool. Rejected: a small model often
doesn't call it and guesses a date from its training data instead, and each
call costs a model round trip out of `max_requests_per_turn`. A time tool can
still be added later if something needs the time of day.

## Risks / Trade-offs

- [The model gets "last week" wrong (for example Sunday-to-Saturday
  instead of Monday-to-Sunday)] → The ranges overlap mostly, the topic
  query still ranks the right conversation first, and results show their
  dates, so the model and the owner can see an off-by-one.
- [Owner waits for the summary after ending a conversation] → One attempt
  only, then the marker. Brainiac is awake at that point, so it is usually a
  few seconds.
- [A 12B model writes a vague title or summary] → The prompt asks for
  concrete outcomes. Keyword search over entries still finds the
  conversation if the summary misses a detail.
- [A large recall crowds the context] → `recall_max_chars`, and only the
  newest recall is sent in full.
- [ragman down] → Search falls back to keywords and says so. Embeddings
  are filled in later by the sweeper.
- [Sweeper backfill after the upgrade wakes brainiac and sends it many
  requests] → One at a time. With the current small number of archived
  conversations, that is a few minutes of work once.
- [A summary includes something the owner would rather forget] → No change
  from today: the entries are already kept. Deleting conversations remains
  out of scope.

## Migration Plan

1. Infra: switch the Postgres image to a pgvector build of Postgres 17 (the
   same major version, so the data directory is reused), and add
   `CREATE EXTENSION IF NOT EXISTS vector;` to the database init script.
   For an existing database, which the init script won't run on again, run
   that statement once as the superuser.
2. Deploy alfred 0.4.0. `alfred migrate` applies `0002`; it fails with a clear
   message if the extension is missing.
3. On start, the sweeper summarises the already archived conversations in
   the background. Wiping the database is not needed. It remains an option
   if the owner prefers to start empty.
4. Rollback: pin 0.3.0, run `alembic downgrade 0001` first (it drops the new
   columns, indexes and recall entries), and remove `[memory]` from the
   config, because 0.3.0 rejects unknown keys. The pgvector image can stay.

## Open Questions

- The exact pgvector image tag for Postgres 17 (for example
  `pgvector/pgvector:0.8.x-pg17-bookworm`). Pick the current one when
  writing the handover update and the test fixture.
