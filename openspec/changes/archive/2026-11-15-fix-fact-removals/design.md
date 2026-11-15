# Design

## Context

See proposal.md for the problem and the measured distances. Current state
(change add-user-facts):

- `FactStore.embed` (`memory/facts.py`) embeds a fact and, in the same step,
  removes its nearest neighbour within `facts_duplicate_max_distance` as a
  duplicate. Two callers: `FactEmbeddingJob` (sweeper, `uses_model = False`,
  runs even while the queue is busy) and `alfred_remember` inline, which
  answers "Already known as fact N" when the new fact lost.
- `FactExtractionJob` (`memory/extract.py`) sends the summary model the
  user's facts as `[7] (learned) lives in Lisbon` and parses
  `NEW:` / `REPLACE 7:` / `NONE` (`memory/extraction.py`). A `REPLACE` id is
  honoured when it is one of the facts shown and still an active learned
  fact of the user.
- The sweeper runs jobs in priority order: summaries, extraction, embedding.
  A job with `uses_model = True` only gets work while the queue is quiet and
  receives `preempt`. The extraction job and `Summaries` each carry their
  own copy of "stream the request, race it against `preempt`, cancel on
  loss".
- Removal is always soft (`removed_at`, `removed_reason`, `replaced_by`).

## Goals / Non-Goals

**Goals:**
- No fact is removed by a rule that measured data shows can't be right.
- A wrong `REPLACE` id costs at most an extra fact, never a lost one.
- The duplicate check costs a model request only when there is something to
  compare.

**Non-Goals:**
- Re-judging the facts that exist today. They are marked as checked by the
  migration; the owner reviews the past removals with the handover's SQL.
- Detecting contradictions in the background. Contradictions stay the job of
  extraction's `REPLACE` and of `alfred_remember`'s `replaces`.
- Changing summaries. `Summaries` keeps its own streaming code.

## Decisions

### D1. Duplicate-check state on `facts`

Migration `0009_fact_duplicate_check` adds `duplicate_checked_at timestamptz
null` (`NULL` = pending), `duplicate_attempts int not null default 0` and
`duplicate_next_at timestamptz null`, and a partial index on `(id) WHERE
removed_at IS NULL AND duplicate_checked_at IS NULL`. It sets
`duplicate_checked_at = now()` on every existing row, so the upgrade starts
no backfill and a fact the owner restores later isn't judged again.

Separate columns from the embedding retry pair, as extraction has its own
pair on `conversations`: a failing model request never delays an embedding
and the reverse. Alternative considered: one "dedupe state" enum. It would
still need its own retry time, so it saves nothing.

### D2. Embedding and checking are two jobs

`FactStore.embed` only stores the vector (it no longer compares).
`FactEmbeddingJob` is unchanged otherwise. A new `FactDuplicateJob`
(`uses_model = True`) runs after it in the sweeper's list:

```
jobs: ConversationSummaryJob, FactExtractionJob, FactEmbeddingJob, FactDuplicateJob
```

`next_due(model_allowed)`: `None` unless the model is allowed; otherwise the
oldest active fact with `duplicate_checked_at IS NULL`, an embedding from
the configured model, and `duplicate_next_at` null or past. A fact without
an embedding isn't due: it waits for the embedding job, which runs even
while the queue is busy, so it is normally embedded long before the queue is
quiet.

`run(fact, preempt)`:

1. Candidates: the user's other active facts with an embedding from the same
   model, within `facts_duplicate_max_distance`, nearest first, at most
   `DUPLICATE_CANDIDATES = 5`. None: mark checked, no model request, done.
2. One streamed request to the summary model with the new fact and the
   candidates (D3), raced against `preempt` like extraction. Preempted:
   record nothing, no attempt counted.
3. On a model or parse error: `duplicate_attempts += 1`, a WARNING
   `fact 88: duplicate check failed (attempt 3): <reason>`, and a retry at
   `backoff_s`. It never gives up: a pending check blocks nothing, and the
   fact is in use meanwhile.
4. Apply under the user's lock (add-user-facts D7). Re-check that the fact is
   still active and pending. For each `SAME` id that is a candidate and still
   active, apply the existing winner rule (`told` over `learned`, else the
   older) and mark the loser `duplicate` with `replaced_by` the winner,
   logging `fact 88 duplicate of 12, removed 88` as today. If the new fact
   loses, stop there. Mark the fact checked and reset its attempts. Log
   `fact 88 duplicate check: 3 candidates, 1 duplicate; <token counts>`,
   with no fact text.

Only the new fact is marked checked; a candidate that survives keeps its own
state. Two facts stored by one extraction are each other's candidates: the
first check settles the pair, and the second fact's check either finds it
removed (nothing to do) or compares it again, which is harmless.

The stream-and-race code moves from `extract.py` into a shared helper in
`memory/` used by both fact jobs. `Summaries` keeps its own copy (non-goal).

Alternative considered: keep embedding-only removal with a tighter default.
The table in the proposal rules it out: Lisbon/Porto (0.047) sits below a
real duplicate (metric, 0.046 to vegetarian/no meat, 0.074) and
likes/refers-to Teach (0.088) sits among them.

### D3. The duplicate-check request

System prompt, in the extraction prompt's style:

> You check a list of short facts about {user} for repeats. Below are a new
> fact and some known facts. Which known facts say the same thing as the new
> fact, so that keeping both would only repeat it? A fact on the same topic,
> one that adds detail the other lacks, or one that contradicts it is not
> the same.
> Answer one line per known fact that is the same: `SAME <id>`. If none is,
> answer with the single line `NONE`.

User message: `New fact: <text>` then `Known facts:` with `[12] <text>`
lines. Origins are left out: which one stays is alfred's rule, not the
model's.

The parser follows `parse_facts`: bullets, numbering, emphasis and code
fences are tolerated, unlabelled lines ignored, `NONE` is an empty answer,
a reply with no `SAME` and no `NONE` is a parse error. Ids that aren't
candidates are ignored.

"Adds detail" counts as different on purpose: "allergic to shellfish" and
"allergic to shellfish and avoids prawns" are both kept, because the winner
rule would otherwise keep the less detailed older fact.

### D4. `REPLACE` quotes the old fact

Reply line: `REPLACE 7 "lives in Lisbon": lives in Porto`. Straight and curly
quotes are accepted, and so is a missing colon after the quote. A `REPLACE`
line without a quote parses as a new fact with a replacement that will be
refused (below), so an old-format reply still stores every fact.

When applying (`extract.py`), a replacement removes fact 7 only if, as
today, 7 was shown to the model and is still one of the user's active
learned facts, **and** the quote matches its text: both are normalised
(case-folded, the user's name dropped when it starts the text, punctuation
dropped, whitespace collapsed) and are equal, their `difflib` ratio is at
least `QUOTE_MIN_RATIO = 0.85`, or the quote's words appear together in the
fact and cover at least `QUOTE_MIN_SHARE = 0.5` of it. The last rule came up
while implementing: models quote a shorter form ("lives in Lisbon" for
"lives in Lisbon, Portugal", ratio 0.77), which a ratio alone would refuse. Otherwise the new fact is stored as a plain
addition, and `conversation 41: REPLACE 7 doesn't quote fact 7; stored as an
addition` is logged at INFO, without either text. The outcome line gains a
count: `conversation 41 facts: 2 added, 1 replaced, 1 replacement refused`.

The quote only confirms the id; alfred doesn't search for the fact the quote
names when the id is wrong. That keeps a wrong id at "one extra fact", which
`/memory forget` cleans up, instead of a guess that could remove another
fact.

`QUOTE_MIN_RATIO`, `QUOTE_MIN_SHARE` and `DUPLICATE_CANDIDATES` are constants, not settings:
they guard the model's output format rather than tune behaviour, and the
log lines show when they misfire.

### D5. What counts as a fact

The extraction prompt gains a test and a list of non-facts. Keep a fact only
if knowing it would change how alfred helps the user in a later conversation
about something else. Not facts:

- instructions to alfred and their settings: setting, changing or cancelling
  reminders and briefings, adding or editing recipes, what to look up, even
  when they describe a schedule ("a weekday briefing at 06:45") — alfred
  keeps those in its reminders and tools already;
- how the user refers to people or things, unless the relationship itself is
  stated ("Teach is her husband" is a fact; "refers to someone as Teach" is
  not);
- what was discussed, asked or done in this conversation.

The `REPLACE` instruction also asks the model to replace only a fact that
the new one makes untrue, not one on a different subject.

### D6. `alfred_remember`

It stores the `told` fact, handles `replaces` as today, embeds it inline (so
the duplicate check has a vector ready), and always answers
`Saved as fact N.` (plus `Replaced 7.`). Its duplicate check happens in the
background. A model request inside the tool would be a second request while
the turn's own is open on a server that runs one at a time, for a check
that can wait.

### D7. Config

`facts_duplicate_max_distance` keeps its name and its `(0, 1)` validation;
its meaning becomes "candidates for the duplicate check", default `0.2`
(the measured real duplicates go up to 0.165). Keeping the name lets an
`alfred.toml` that sets it keep loading; the handover update says to change
or drop an explicit `0.1`.

## Risks / Trade-offs

- [The model calls two different facts the same] → the prompt counts added
  detail and contradiction as different, only candidates can be named, every
  removal is logged with both ids, and removals stay restorable.
- [A correct `REPLACE` with a loosely paraphrased quote is refused] → the
  outdated fact stays next to the new one; the refusal is logged and
  `/memory forget` removes it. Preferred over removing the wrong fact.
- [Duplicates linger until the queue is quiet] → they only cost prompt space
  in the meantime; extraction already avoids repeating known facts.
- [More model requests] → only for facts with a close neighbour, only while
  the queue is quiet, preempted by any queue item.
- [A stricter prompt drops a useful fact] → `alfred_remember` stores what the
  user asks for regardless; the backfill log lines show the counts.

## Migration Plan

1. Deploy 0.15.0; migration `0009` runs on start and marks every existing
   fact as checked. No backfill starts.
2. If `alfred.toml` sets `facts_duplicate_max_distance = 0.1`, change it to
   `0.2` or remove it.
3. Review the past removals with the handover's SQL and restore the wrong
   ones; `/memory forget` the useless facts.

Rollback: downgrade to `0008` with the 0.15.0 image (drops the three
columns), then pin 0.14.0. Facts removed by the new check stay removed.
