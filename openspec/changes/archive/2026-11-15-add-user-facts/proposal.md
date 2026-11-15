# Proposal

## Why

alfred remembers *conversations*: they are summarised, searched and recalled. It
doesn't know the *user*. A detail mentioned once ("I'm allergic to shellfish")
only comes back if the model decides to search for it, and nothing makes it
search for allergies while it helps pick a restaurant. Durable facts about each
user, always in the model's context, close that gap. The idea comes from
OpenJarvis's automatic memory. This change keeps its idea and fixes its
weaknesses: per-exchange extraction, exact-text dedupe, no contradiction
handling, substring deletes and regex injection scanning.

## What Changes

- **Facts per user.** Each user has a list of short facts with an id, text and
  origin: `told` (the user asked alfred to remember it) or `learned` (extracted
  from a conversation). No user can see or change another user's facts.
- **Automatic extraction** when a conversation is archived. A new sweeper job
  gives the model the user's own messages and alfred's replies from that
  conversation, never notices, `/ask` prompts or reminder texts, together with
  the user's current facts. The model returns new facts and the ids of
  `learned` facts they replace. Extraction waits for a quiet queue and gives way
  to queue items, like summaries. The model's selectivity is the only
  filter: alfred stores every fact it returns. A user's conversations are
  extracted in order. A failing conversation is retried with a WARNING on each
  failure, and given up after a configured number of attempts (0, the default,
  means never, so a user's later extractions wait for it). Existing archived
  conversations are backfilled, oldest first.
- **Removed facts are kept for audit.** Forgotten, replaced and duplicate facts
  are marked removed with when, why and what replaced them, and are never
  shown to the model or in `/memory` again.
- **Near-duplicate check.** Each fact is embedded through the embedding service
  (ragman). A new fact very close to one of the same user's existing facts is
  removed as a duplicate, keeping `told` over `learned` and older over newer.
- **The sweeper runs a list of jobs**: summary and conversation embedding
  (unchanged), fact extraction, fact embedding. Each job has its own retry
  state.
- **Two new tools in chat turns:** `alfred_remember(text, replaces?)`, offered
  always, and `alfred_forget(ids)`, offered only while facts are in the context.
- **Facts in the context, behind a flag.** With `[memory] facts_in_context`
  on (off by default), every turn of a user carries all of that user's facts
  in the system prompt, within a character budget (`told` first, then
  `learned` newest first). When facts don't fit, the block says how many are
  shown and points to `/memory`. Extraction, the tools and `/memory` work
  whether the flag is on or off.
- **`/memory` command**, handled without the model. It lists the user's facts
  with their ids, and `/memory forget <id>...` removes facts (kept for audit). This is alfred's
  first bot command. Every other `/` text still goes to the AI.
- **Context log** shows the facts on the turn's size line: count and
  characters, `facts off`, `facts none`, or `N of M ... over budget`, plus a
  WARNING when facts are dropped for lack of space.

## Capabilities

### New Capabilities
- `user-facts`: durable per-user facts: storage, extraction from archived
  conversations, near-duplicate check, the remember/forget tools, the facts
  block in the system prompt with its flag and budget, and the `/memory`
  command.

### Modified Capabilities
- `telegram-chat`: "No bot commands" allows exactly one command, `/memory`.
- `agent-loop`: "Turn context size is logged" includes the facts state and size.
- `users`: "Users don't see each other's data" includes facts.
- `service-configuration`: "All tunables in the file" lists the new `[memory]`
  fact settings.

## Impact

- **Code:** new `src/alfred/memory/facts.py` (store, extraction, embedding and
  dedupe) and fact tools. `memory/sweeper.py` becomes a loop over jobs.
  `agent/prompts.py` and `agent/turn.py` add the facts block.
  `agent/context.py` logs it. `telegram/bot.py` handles `/memory`. Also
  `tools/catalog.py`, `app.py` (`build_toolsets`, wiring) and `config.py`.
- **Database:** migration `0007`: a `facts` table (with a `vector` embedding
  like conversations), and fact-extraction state columns on `conversations`.
  Every archived conversation starts with extraction pending (the backfill).
- **Model server:** one extra request per archived conversation on `ai.lan`,
  only when the queue is quiet. After the upgrade, a one-off backlog covering
  the conversations since 1 Nov 2026.
- **Embedding service:** one request per new fact on ragman.
- **Config:** new `[memory]` keys, documented in `alfred.toml.example` and the
  README. No new secrets. No new dependencies.
- **Handover:** an update for the release (migration, new keys, flag off by
  default).
- **Out of scope:** token counts for facts, homelab docs, choosing facts by
  relevance, and moving the search fusion code into a shared helper.
