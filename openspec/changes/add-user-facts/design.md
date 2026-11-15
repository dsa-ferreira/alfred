# Design

## Context

See proposal.md for the motivation and specs/ for the behaviour. Current state
that shapes the approach:

- `Sweeper` (`memory/sweeper.py`) is one task over one kind of work. It picks
  the oldest archived conversation missing a summary or an up-to-date
  embedding, waits for a quiet queue before a model request (`_quiet_in`,
  `QueueActivity`), passes `activity.arrived` as `preempt`, and sleeps until
  the next `summary_next_at`, a quiet queue, or a nudge
  (`Summaries.changed`, `activity.changed`). Summary and embedding share one
  retry pair (`summary_attempts`, `summary_next_at`). Only the sweeper calls
  `Summaries.summarise`.
- `Summaries._make_summary` already has the pattern extraction needs:
  streamed request (`complete_streamed`), a race against `preempt`, cancel and
  await on loss, a token-count log line and `_retry_later` with `backoff_s`.
- `transcript.render` turns entries into `User:` / `alfred:` /
  `[notice:x]` / `[ask:x]` / `[reminder:#n]` lines. The entry kinds are
  `user`, `assistant`, `ask_prompt`, `ask_reply`, `notice`, `reminder`,
  `reminder_reply` and `recall`.
- `Embedder` (ragman) has `embed_document` / `embed_query` with nomic
  prefixes. Conversations store `Vector()` with no fixed dimension plus
  `embedding_model`, and are re-embedded when the model changes.
- `system_prompt` (`agent/prompts.py`) is persona, operating rules, today,
  brain, toolset notes, then the ask/reminder rule. `context_sizes`
  (`agent/context.py`) logs labelled `PromptNote`s inside the system size.
- `telegram/bot.py`'s `on_message` sends every text message to
  `queue.enqueue_user`.
- Toolsets follow `alfred.tools.base` (AGENTS.md "Adding a tool group").
  `MemoryTools` is `chat_only`.

## Goals / Non-Goals

**Goals:**
- Extraction reuses the summary machinery (quiet queue, preemption, backoff)
  instead of building a second one.
- One write path per user's facts, safe against the sweeper, tools and
  `/memory` running at the same time.
- The facts block changes only when the user's facts change, so the model
  server's prompt-prefix cache survives between turns.

**Non-Goals:**
- Several alfred processes (the queue-activity signal is in-process, as today).
- Choosing facts by relevance, token counts, homelab docs (proposal, Impact).
- A way to see or restore removed facts from Telegram. They are kept for audit
  in the database only.
- Changing the summary or conversation embedding behaviour.

## Decisions

### D1. Data model: a `facts` table and extraction state on `conversations`

Migration `0007_user_facts`:

- `facts`: `id bigint` identity primary key, `user_name text not null`,
  `text text not null`, `origin text not null` CHECK `IN ('told','learned')`,
  `created_at timestamptz default now()`, `conversation_id bigint null`
  (references `conversations`, `ON DELETE SET NULL`; the source of a `learned`
  fact), `embedding vector null`, `embedding_model text null`,
  `embedding_attempts int default 0`, `embedding_next_at timestamptz null`,
  and for soft delete `removed_at timestamptz null`, `removed_reason text null`
  CHECK `IN ('forgotten','replaced','duplicate')`, `replaced_by bigint null`
  (references `facts`). A CHECK keeps `removed_at` and `removed_reason`
  both set or both null. Partial index `(user_name, id) WHERE removed_at IS
  NULL`: every read except audit uses it.
- `conversations`: `facts_outcome text null` CHECK `IN ('done','gave_up')`
  (`NULL` = pending), `facts_attempts int default 0`,
  `facts_next_at timestamptz null`. Partial index on `(user_name, id)` where
  `ended_at IS NOT NULL AND facts_outcome IS NULL`.

Every existing archived conversation gets `facts_outcome = NULL`. That *is*
the backfill: there's no special code.

Separate retry columns, not the shared `summary_*` pair, so a failing
extraction never pushes back a summary and the reverse. Alternative
considered: one generic `jobs` table. It would be cleaner in the abstract, but
it duplicates state the rows already carry, and the existing summary columns
would stay anyway.

### D2. The sweeper loops over jobs

```
 Job (protocol)
   uses_model: bool
   next_due(model_allowed) -> key | None
   seconds_until_due(model_allowed) -> float | None
   run(key, preempt) -> None

 jobs, in priority order:
   ConversationSummaryJob   (today's Summaries: summary + embedding)
   FactExtractionJob        uses_model
   FactEmbeddingJob         embedding + dedupe
```

Each round: compute `quiet_in` as today. `model_allowed = quiet_in == 0`. Run
the first job whose `next_due(model_allowed)` returns a key. Pass `arrived` as
`preempt` when the job uses the model. With nothing due, sleep until the
smallest `seconds_until_due` across jobs, capped by `quiet_in`, waking on any
job's `changed` or `activity.changed`. The existing "same key due right after
running it" guard becomes per job.

`ConversationSummaryJob` wraps today's logic unchanged: when the model isn't
allowed it returns only embedding-only conversations, which is exactly today's
`embedding_only`. Summary comes before extraction because summaries feed
search, which users see.

Alternative considered: a second sweeper task for facts. Two tasks would each
need their own quiet check, and could both start a model request in the same
quiet moment. The model server runs one request at a time, so one loop is
simpler.

### D3. Extraction, in order per user

`next_due`: the oldest conversation with `ended_at IS NOT NULL`,
`facts_outcome IS NULL`, `facts_next_at` null or past, **and** no older
conversation of the same user with `facts_outcome IS NULL`. The `NOT EXISTS`
uses the partial index. A conversation that is waiting on its own backoff
therefore blocks only its user's later conversations.

`run`:

1. Load the entries. Keep only kinds `user` and `assistant` (D4). If no `user`
   entry is left, mark `done` with no model request.
2. Render them with `render`, `cut_middle` to `summary_input_max_chars`, and
   load the user's current facts (all of them, not budgeted).
3. Send a streamed request to the summary model (`Config.summary_model`), racing
   `preempt` exactly as `_make_summary` does. On preemption, record nothing.
4. Parse (D5). On a model or parse error: `facts_attempts += 1` and log a
   WARNING `conversation 41: fact extraction failed (attempt 3): <reason>`. If
   `facts_extraction_max_attempts > 0` and attempts reached it, set `gave_up`
   and log a WARNING saying so. Otherwise set `facts_next_at` with
   `backoff_s`. With the default `0`, a conversation that always fails retries
   forever at `retry_max_s` intervals and holds back its user's later
   conversations. The per-attempt WARNING is how you notice that.
5. Apply in one transaction under the user's lock (D7). Re-check that the
   conversation is still pending. Insert **every** new fact as `learned`, each
   cut to `fact_max_chars`. There's no per-conversation cap: the prompt asks
   the model to be selective, and alfred doesn't second-guess it. A replaced id
   that is one of this user's active `learned` facts gets `removed_reason =
   'replaced'` and `replaced_by` the new fact. Anything else becomes a plain
   addition. Set `done` and reset attempts. Log
   `conversation 41 facts: 2 added, 1 replaced` with the token counts.
6. Nudge `FactEmbeddingJob`, which embeds and dedupes the new facts right
   away, since it doesn't wait for a quiet queue.

The extraction request reuses `summary_input_max_chars` and the summary model.
It's the same transcript size problem on the same server, so no new knobs.

### D4. The structural filter

Only `user` and `assistant` entries reach extraction. `ask_reply` and
`reminder_reply` are alfred's words too, but they are written in response to
service or other-user text. Leaving them out keeps the rule simple: only what
this user said, and what alfred answered them directly.

`assistant` replies can still paraphrase a web page or a notice that was
earlier in the same conversation. The prompt tells the model to take facts
only from what the user says about themselves, using alfred's replies only to
understand those messages. That is a prompt-level defence, not structural, and
is listed under Risks.

### D5. Extraction prompt and reply format

The system prompt follows OpenJarvis's extractor: stable over time;
preferences, identity, goals, ongoing projects, constraints, relationships;
ignore one-off tasks, small talk and what alfred says about itself; facts in
the third person about the named user. The user message holds the user's
current facts (`[7] (learned) lives in Lisbon`, `told` marked as not
replaceable), then the filtered transcript.

Reply format, one per line, labelled like the summary reply:

```
NEW: works night shifts on weekends
REPLACE 7: lives in Porto
NONE
```

The parser tolerates bullets, numbering, markdown emphasis and code fences
(the same approach as `_LABEL` in `parse_summary`). It ignores unlabelled
lines. A reply with no `NEW`/`REPLACE` lines and no `NONE` is a parse error.
A `NONE` reply is a success with nothing to store. Labelled lines are used
instead of JSON because small models wrap JSON in prose, and the summary
parser already handles labelled lines from this model.

### D6. Embedding and near-duplicates

`FactEmbeddingJob.next_due`: the oldest fact whose embedding is null or from
another model, with `embedding_next_at` null or past. `run` calls
`embed_document(text)`. On failure it backs off on the fact's own columns,
with no give-up: ragman being down is temporary, and the fact works without
an embedding. On success, under the user's lock: store the vector, then find
the nearest *other active* fact of the same user with the same
`embedding_model` by `cosine_distance`. If it's within
`facts_duplicate_max_distance`, mark the loser removed as `duplicate` with
`replaced_by` the winner (`told` beats `learned`, otherwise the older wins),
and log `fact 88 duplicate of 12, removed 88`. Removed facts are never
embedded again or compared.

`alfred_remember` calls the same embed-and-dedupe step inline after inserting,
so a duplicate is normally caught before the tool returns ("already known as
fact 12"). If ragman fails, the tool still succeeds and the job retries later.
That gives one dedupe code path with two callers.

Default distance 0.1 (cosine similarity 0.9) for `nomic-embed-text`. It's a
starting value to tune from the dedupe log lines, hence configurable.

### D7. One lock per user's facts

Every write to a user's facts takes
`pg_advisory_xact_lock(hashtextextended('facts:' || user_name, 0))` inside its
transaction: extraction apply, dedupe, `alfred_remember`, `alfred_forget` and
`/memory forget`. Every removal is an UPDATE that sets `removed_at`,
`removed_reason` and `replaced_by` on an active row. Nothing is ever deleted,
and "active" (`removed_at IS NULL`) is part of every lookup by id, so a
removed id reads as not found. Embedding requests and model requests happen *outside* the
lock. Only the database work is serialised. This keeps "replace 7" and
"forget 7" running at the same time from double-deleting or reviving a fact,
without locking rows that might not exist yet.

### D8. Tools

New `FactTools(LocalToolset)` in `memory/facts.py`, `chat_only`:

- `alfred_remember(text: str, replaces?: int[])`. Text is stripped. Empty or
  longer than `fact_max_chars` gives an error result. All `replaces` ids must
  be this user's facts, or nothing happens and the error names the missing ids
  (the same text for unknown and foreign ids). Returns `Saved as fact 15.`
  (plus `Replaced 7.`) or `Already known as fact 12.`
- `alfred_forget(ids: int[])`, offered only when `facts_in_context`. It
  removes the user's matching facts and reports `Forgot 12, 15. Not found:
  99.`

Both are added to `tools/catalog.py` and to `build_toolsets` after
`MemoryTools`. When `facts_in_context` is on, a prompt note explains the
facts block's `told`/`learned` labels and that ids are for `alfred_forget` and
`replaces`.

### D9. The facts block

Built in `turn.py` from one query per turn (the user's facts by id), and
appended as the **last** part of the system prompt, after the ask/reminder
rule. A fact change then only invalidates the tail of the cached prefix.

Selection: `told` newest first, then `learned` newest first, adding facts
while the running total of rendered lines fits `facts_context_max_chars`
(greedy, skipping a fact that doesn't fit and trying smaller ones). The
chosen facts are rendered in id order:

```
What you know about diogo (all 14 facts):
[12] allergic to shellfish (told)
[15] runs a homelab, services on *.lan (learned)
```

```
What you know about diogo (40 of 52 facts; the rest are listed by /memory):
```

On ask and reminder turns the block is there too. Facts help there ("call mum"
plus the mum's-number fact), and only the tools are chat-only.

### D10. Context log

`context_sizes` gains a `facts` argument: `None` (off), or
`(shown, total, chars)`. The system part becomes, for example:

```
system 9120 chars (facts 14, 1240 chars; instructions: mcparr 830); ...
system 7880 chars (facts off; instructions: mcparr 830); ...
system 7880 chars (facts none); ...
system 10950 chars (facts 40 of 52, 2990 chars, 12 left out; ...); ...
```

When facts are left out, `turn.py` also logs a WARNING:
`turn 12: 40 of 52 facts fit [memory] facts_context_max_chars 3000`.

### D11. `/memory`

In `on_message`, before enqueuing, text matching
`^/memory(@\w+)?(\s+(.*))?$` (so `/memory@alfred_bot` from Telegram's command
menu works, and `/memorybank` doesn't) goes to a `MemoryCommand` handler.
It reads or writes the facts directly and replies with `parse_mode=None`. It is
not queued, not recorded and doesn't touch idle close, so it answers even
while a turn runs.

- `/memory` gives a header in the butler register with the user's address
  (`notices.py`, like the other fixed texts), then
  `[12] allergic to shellfish (told, 3 Nov)` lines in id order, split with
  the existing Telegram splitting.
- `/memory forget 12 15` runs under D7's lock and names each removed fact and
  each id not found.
- Anything else gives a usage text naming both forms.

The command is registered with Telegram (`set_my_commands`) at startup, so it
shows in the client's menu. This is the only command.

### D12. Config

`MemoryConfig` gains: `facts_in_context: bool = False`,
`facts_context_max_chars: PositiveInt = 3000`, `fact_max_chars: PositiveInt =
300`, `facts_extraction_max_attempts: NonNegativeInt = 0` (0 = never give
up),
`facts_duplicate_max_distance: float` in `(0, 1)`, default `0.1`.

## Risks / Trade-offs

- [alfred's replies can carry text from web pages or notices into extraction]
  → the prompt limits facts to what the user says about themselves. The flag
  ships off, so learned facts can be reviewed with `/memory` before they reach
  any prompt. The `learned` label tells the model they may be wrong.
- [A small model extracts junk or one-off details, and with no cap a chatty
  reply stores all of it] → the backfill on two weeks of conversations is the
  quality check, with the flag off. The fix is the prompt, not a cap.
  `/memory forget` and `alfred_forget` clean up, and removed facts stay in the
  database to judge extraction by.
- [Stale learned facts nothing contradicts] → accepted for now. Supersede
  covers contradictions. A cleanup or "retire old facts" pass is a later
  change if `/memory` shows it's needed.
- [A conversation that keeps failing blocks its user's later extractions; by
  default forever] → a WARNING on every failed attempt with the attempt count.
  Setting `facts_extraction_max_attempts` turns on giving up. Preemptions
  don't count as attempts.
- [Removed facts accumulate] → they're short text, a handful per user per
  week. Every read filters on the partial index of active facts.
- [The extraction prompt grows with the user's facts] → facts are short and
  only active ones are sent. If it becomes a problem, the context-size and token
  logs will show it first.
- [Dedupe threshold wrong for the embedding model] → configurable. Each removal
  is logged with both ids, so a wrong threshold is visible.
- [The facts block in every prompt costs context] → budgeted, logged on every
  turn, and off by default.

## Migration Plan

1. Deploy with migration `0007` applied by the existing startup migration. The
   flag is off. The sweeper starts extracting all archived conversations,
   oldest first, whenever the queue is quiet.
2. Watch the `conversation N facts:`, `fact N duplicate` and extraction
   failure log lines, and check `/memory`.
3. Set `[memory] facts_in_context = true` and restart.

Rollback: set the flag off (facts leave every prompt at once). Full rollback:
downgrade to `0006`, which drops `facts` and the three conversation columns.
