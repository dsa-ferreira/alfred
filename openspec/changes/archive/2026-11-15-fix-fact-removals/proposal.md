# Proposal

## Why

User facts are being removed for facts they have nothing to do with. "umbs
likes Teach very much." was removed as a duplicate of "umbs has a person she
refers to as Teach.", and "teach is adding new low-calorie, high-protein
recipes to their cookbook." was replaced by "teach lives in Lisbon,
Portugal". Extraction also keeps facts that don't help: trivia ("has a
person she refers to as Teach") and instructions to alfred ("prefers a
weekday briefing at 06:45 ...", from a message that changed a reminder).

Measured with the deployed embedding model (`nomic-embed-text:v1.5`),
embedding distance can't tell a duplicate from a related or contradicting
fact about the same person:

| Pair | Distance | Should be |
|---|---|---|
| lives in Lisbon / lives in Porto | 0.047 | replaced |
| prefers metric / always wants metric | 0.046 | duplicate |
| is vegetarian / does not eat meat | 0.074 | duplicate |
| likes Teach / refers to someone as Teach | 0.088 | both kept |
| allergic to shellfish / the user is allergic to shellfish | 0.165 | duplicate |
| allergic to shellfish / is vegetarian | 0.174 | both kept |

No threshold separates them, so the duplicate check removes by a rule that
can't be right. The recipes/Lisbon pair is 0.365 apart: that removal was the
extraction model naming the wrong id in a `REPLACE` line, which alfred
trusts as long as the id is one of the user's learned facts.

## What Changes

- **Duplicates are decided by the model.** Embeddings only pick candidates:
  the user's other active facts within the configured distance. A model
  request then says which candidates, if any, say the same thing as the new
  fact. Only those are removed, with the same winner rule as before (`told`
  over `learned`, else the older). A fact with no candidate is done without a
  model request. The check runs in the background memory sweeper, after
  extraction, only while the queue is quiet, and gives way to queue items.
- **BREAKING (config meaning):** `memory.facts_duplicate_max_distance` now
  sets how close a fact must be to be *checked*, not to be removed. Its
  default goes from 0.1 to 0.2, which takes in the measured real duplicates.
- **`alfred_remember` no longer answers "Already known as fact N".** The
  duplicate check needs a model request, which can't run inside the turn
  that called the tool. Remembered facts are checked in the background like
  learned ones.
- **`REPLACE` must quote the fact it replaces.** The extraction reply line
  becomes `REPLACE 7 "lives in Lisbon": lives in Porto`. alfred removes fact
  7 only if it is one of the user's active learned facts and the quote
  matches its text; otherwise the new fact is a plain addition and the
  mismatch is logged.
- **Stricter extraction prompt.** A fact is kept only if knowing it would
  change how alfred helps the user in a later, unrelated conversation.
  Instructions to alfred (setting or changing reminders, adding recipes,
  lookups) are tasks, not facts, even when they describe a schedule, and so
  are trivia about how the user names people or things.
- **Restoring wrongly removed facts.** Facts are only ever marked removed, so
  the 0.15.0 handover update gives the owner SQL to review the facts removed
  as duplicates or replaced, and to restore chosen ones.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `user-facts`: duplicates are confirmed by the model, in the background and
  only while the queue is quiet; `REPLACE` needs a matching quote; the
  extraction prompt's notion of a durable fact is narrower; `alfred_remember`
  no longer reports duplicates.
- `service-configuration`: the duplicate distance selects candidates for the
  check, with a new default of 0.2.

## Impact

- Code: `memory/facts.py` (dedupe split into candidate search and a model
  check, a new sweeper job), `memory/extraction.py` (prompt, `REPLACE`
  format and parser, duplicate-check prompt and parser), `memory/extract.py`
  (quote check when applying), `app.py` (job wiring), `config.py` (default).
- Database: migration `0009` adds the duplicate-check state to `facts`;
  existing facts start as checked, so nothing is re-judged on upgrade.
- ollama-gpu: one extra request per new fact that has a close neighbour,
  only while the queue is quiet.
- Docs: README, `alfred.toml.example`, handover update 0.15.0 with the
  restore SQL and the changed default.
