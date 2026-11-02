# Proposal

## Why

When the owner ends a conversation, alfred archives it and never looks at it
again. Things worked out earlier, like a recipe, a fix or a decision, are
lost to the model, and the owner has to explain them again. The owner wants
to be able to say "remember conversation 41" or "remember when we talked
about the new chicken recipe" and have alfred pick that content back up.

## What Changes

- When a conversation ends, alfred asks the model for a short title and a
  one- or two-sentence summary of it, embeds them with the embedding service
  on `ragman.lan`, and stores both with the conversation.
- The reset marker carries ids and the title:
  `— #41 "Chicken thighs recipe" —` followed by `— new conversation #42 —`.
  If summarising fails, the marker shows only the ids, and the summary is
  retried in the background with no further message.
- Two new local tools, offered only in owner turns:
  - `alfred_search_conversations`: finds archived conversations by meaning
    (vector similarity over title + summary), by keywords (full-text
    search over title, summary and entries), and by date range, with or
    without a topic ("the chicken recipe last week", "what did we talk about
    on Tuesday?").
  - `alfred_recall_conversation`: brings an archived conversation into the
    current one.
- A recall is a new kind of conversation entry: a full transcript of the
  recalled conversation, capped in size. It stays in the context window like
  a service notice, so follow-up questions keep working. Only the newest
  recall in the window is sent in full; older ones are sent as their summary.
- Every turn's system prompt states today's date and weekday (no time of
  day), so the model can resolve "last week" or "yesterday". New setting
  `[ai] timezone`, defaulting to `Europe/Lisbon`.
- New `[memory]` config section: summary model, embedding endpoint, model
  and prefixes, recall and summariser size caps, and number of search
  results.
- **BREAKING (deployment):** the database needs the `pgvector` extension.
  The Postgres image changes to a pgvector build, and the extension is
  created by the database init script, because alfred's role is not a
  superuser. Existing data is kept; archived conversations are summarised in
  the background after the upgrade.

## Capabilities

### New Capabilities

- `conversation-memory`: summaries of archived conversations, searching
  them, and recalling one into the current conversation.

### Modified Capabilities

- `conversation-history`: the reset sequence now summarises before the
  marker, and the marker shows ids and the title. Archived conversations may
  reach the model through a recall. The context window counts recall
  entries and sends only the newest one in full.
- `agent-loop`: the operating rules also explain `[recall:…]` messages, and
  every system prompt states the current date.
- `service-configuration`: the list of tunables gains the owner's timezone
  and the `[memory]` settings.

## Impact

- Code: `src/alfred/history.py` (recall entries, window rendering),
  `src/alfred/queue.py` (reset sequence and marker),
  `src/alfred/agent/turn.py` and `prompts.py` (new local tools, operating
  rules), `src/alfred/config.py` (`[memory]`), new modules for summarising,
  embedding and search, `src/alfred/app.py` (wiring and the background
  summary task).
- Database: migration `0002` adds summary, title and embedding columns to
  `conversations`, a full-text index on entries, a new `recall` entry kind
  and the recalled conversation reference. Requires the `vector` extension.
- Dependencies: the `pgvector` and `tzdata` Python packages. Tests use a pgvector Postgres
  image.
- External services: `http://ragman.lan` (`/v1/embeddings`, model
  `nomic-embed-text:v1.5`). Already reachable through nginx; no
  infrastructure change needed for access.
- Model load: one extra model request when a conversation ends. The owner's
  next message waits for it, because the queue worker is serial.
- Deployment: release 0.4.0 and a handover update (image tag, Postgres
  image, init script, `[memory]` keys with recommended values).
