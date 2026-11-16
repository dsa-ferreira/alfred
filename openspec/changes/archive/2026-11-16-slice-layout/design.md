# Design

## Context

See proposal.md for why. The current state that shapes the approach, measured
with an AST pass over `src/alfred/`:

- **Upward edges.** `config` -> `tools.catalog` -> `attachments.tools`,
  `memory.facts`, `memory.search`, `reminders.tools`, `web.tools`,
  `tools.*` (lazy import). Also `config` -> `tools.base` (`AllowList`) ->
  `config` (`User`, type-only).
- **Features reaching across.** `history` knows input-request statuses
  (`_LIVE_STATUSES`, `_not_parked` joins `input_requests`).
  `memory.sweeper` imports `queue` (type-only). `tools.dates` and
  `agent.turn` import `reminders.when` for time formatting. `inputs.loop`
  imports `reminders.loop` for `seconds_to_next_check`.
- **Concepts without a home.** Conversations are spread across
  `history.py`, `db/conversations.py`, `idle.py`, `tools/conversation.py`,
  `memory/transcript.py` (with `load_transcript` in `memory/summaries.py`)
  and `notices.reset_marker`.
- **Multi-job modules.** `tools/base.py` (turns, toolset contract, access,
  questions, model images), `memory/facts.py` (locking, store, embedding job,
  tools), `memory/search.py` (search, tools), `memory/sweeper.py` (job
  runner, summary job), `memory/extraction.py` (extraction prompt, duplicate
  prompt), `config.py` (every section, loading, cross-checks),
  `agent/turn.py` (checkpoint, results, agent, times).
- `memory/streaming.py` says "`Summaries` keeps its own copy of this".
- `scripts/check_layout.py` check 5 sees only cycles between packages, so
  it missed the lazy `config` -> feature edge.

## Goals / Non-Goals

**Goals:**

- A module's package tells you its rank, and the import rules between ranks
  are checked by `make lint`.
- Each feature is one package; each module in it has one job.
- Each group of tasks leaves `make lint` and `make test` green.

**Non-Goals:**

- No database change: `db/models.py` stays whole until `per-feature-schema`.
- No change to how turn kinds work: `kinds.py` moves into `turns/` as it
  is; `turn-kinds` reworks it.
- No behaviour, config-key or message-text change.

## Decisions

### D1. Ranks

```
 rank  name       packages / modules
 ----  ---------  ---------------------------------------------------------
  6    entry      app, cli, config, startup
  5    adapters   telegram | http
  4    runtime    turns
                  agent
  3    features   archive | facts | reminders | web | mcp | toolbox
                  conversations
                  attachments
                  brains
  2    kernel     kernel
  1    infra      db | llm | settings | backoff | times
```

A module imports only from its own package or from rows below it. `a | b`
on one row means they are siblings that must not import each other.
Inside rank 3 and rank 4 the rows are ordered too: `conversations` may use
`attachments`, but not the other way round.

The feature order comes from the edges that are real today, not from
taste:

- `archive` and `facts` both read conversation transcripts, so they sit
  above `conversations`.
- `conversations` renders attachment handles into transcripts and history,
  so it sits above `attachments`.
- `attachments` checks whether the current brain has vision, so it sits
  above `brains`.

So a feature may depend on another feature, but only downwards, along a
declared order. "Features never import each other" was the first idea; the
code shows three real dependencies, and a declared order is honest about
them while still forbidding cycles.

Alternative considered: ranks as directories (`alfred/features/reminders`).
Rejected: deeper import paths for no extra checking. The contract file
states the ranks.

### D2. Package map

Moves use `git mv` so history follows. Names may change during
implementation; what's fixed is the rank of each item and the one-job
split.

```
infra
  settings.py        Section base, validators (_Name, _HttpUrlStr, ...), ConfigError
  times.py           format_when, format_day_time, format_now, parse_date,
                     seconds_to_next_check     (from reminders/when, reminders/loop)
  backoff.py         backoff_s(attempts, initial_s, max_s)  (no QueueConfig)
  llm/chat.py        ModelClient, ModelReply, ToolCall, errors   (from model.py)
  llm/embedding.py   EmbeddingClient                             (from memory/)
  llm/streaming.py   complete_unless_preempted (the only copy)
  llm/protocols.py   ChatModel, Embedder   (from memory/summaries: Model, Embedder)
  db/                engine, migrate, models (unchanged this change)

kernel                                                   (from tools/base.py)
  turn.py            ChatTurn, AskTurn, ReminderTurn, TurnInput, Resume
  toolset.py         Toolset, OpenToolset, LocalToolset, ToolOutcome,
                     AttemptContext, PromptNote, parse_arguments, dispatch*,
                     chat_only, LOCAL_GROUP
  access.py          AllowList, Gated, Confirmed
  questions.py       Question, Option, InputNeed, DO_IT, CANCEL
  content.py         ModelImage
  users.py           User, Users                         (from config.py)
  outbox.py          Outbox                              (from outbox.py)

brains/        registry.py (Brain, Brains), tools.py (alfred_set_model), config.py
attachments/   as today + config.py, notices.py; tools.py takes a scope (D5)
conversations/ store.py (History, HistoryEntry)          (from history.py)
               current.py                                (from db/conversations.py)
               transcript.py (+ load_transcript)          (from memory/)
               idle.py, tools.py, notices.py, config.py
archive/       summaries.py, summary_job.py, search.py, tools.py,
               prompts.py (summary prompt and parser), config.py
facts/         store.py, embedding_job.py, extraction_job.py,
               extraction_prompt.py, duplicate_job.py, duplicate_prompt.py,
               tools.py, notices.py, config.py
reminders/     when.py (resolve, Rule, occurrences), store.py, tools.py,
               loop.py, notices.py, config.py
web/, mcp/     as today + config.py
toolbox/       calculator.py, dates.py

agent/         turn.py (Agent), results.py (TurnResult, TurnFailed, Fallback),
               checkpoint.py, context.py, inline.py, messages.py, prompts.py,
               notices.py, config.py
turns/         queue.py, worker.py, kinds.py, background.py (the sweeper's
               job runner), notices.py, config.py,
               inputs/ store.py, parking.py, loop.py, titles.py, notices.py

telegram/      bot.py, formatting.py, questions.py (HTML, keyboard; from
               inputs/render), taps.py (from inputs/callbacks), notices.py
http/          api.py, config.py

entry          app.py, cli.py, config.py (file model, Config, loaders),
               startup.py (allow-list names, unknown users)
```

`tools/` and `memory/` disappear, along with the root modules `history`,
`idle`, `kinds`, `model`, `notices`, `outbox`, `queue`, `worker`, `api`
and `brains`.

### D3. Config: sections in features, composition at the entry rank

- Each feature defines its section model in its own `config.py`, built on
  `settings.Section`. The validators move to `settings.py`.
- `alfred/config.py` keeps the file model, `Config`, `load_config`,
  `load_database_url` and the checks that span sections (users against
  servers, token resolution, duplicate tokens). It imports the feature
  sections; nothing below rank 6 imports it.
- Code below the entry rank gets the section it needs (or plain values,
  such as the time zone) from `app`. Today `agent`, `web.tools`,
  `mcp.toolset`, `reminders.tools` and the `memory` modules take the whole
  `Config`.
- **Shared TOML tables.** Two tables feed several slices:
  - `[memory]` feeds `archive`, `facts` and embedding settings in `llm`;
  - `[ai]` feeds `brains` (model, vision, models), `llm` (base URL,
    timeout) and `agent` (persona, request limits, time zone).

  The file model's section class for each table inherits from the
  feature-owned sections (`class _MemorySection(ArchiveConfig,
  FactsConfig, EmbeddingConfig)`). One object is parsed, and each feature
  receives it typed as its own part. The TOML shape doesn't change. A test
  loads `alfred.toml.example` and checks every key still lands.
- `Config.summary_model` moves to `archive/config.py`, and
  `Config.servers_for` to `mcp/config.py`.

Alternative considered: separate dataclasses per feature, filled from the
file model by hand. Rejected: every key would be written twice.

### D4. The tool-name check moves to startup

- `LocalToolset` subclasses declare `names: ClassVar[frozenset[str]]`:
  every name the toolset can ever offer. They already have `NAMES`-style
  constants.
- `startup.check_tool_names(users, toolset_types)` is called from `app`
  before any connection opens. It runs on the classes `_build_toolsets`
  will build, so the list of toolsets lives in one place.
- It raises `ConfigError` with today's message (`users[i] (<name>): tools
  entry '...' is not an alfred tool`). The CLI catches it with the same
  exit code (2) as a config error. The test of this moves from
  `tests/config/` to the startup tests, asserting the same message.
- `tools/catalog.py` and AGENTS.md's "add the name to the catalog" step
  disappear. A new toolset is checked as soon as it's added to `app`.

Alternative considered: a registry filled by `__init_subclass__`. Rejected:
an import side effect that decides behaviour.

### D5. Breaking the edges that go across or up

| Edge today | After |
|---|---|
| `history` joins `input_requests` to hide other parked turns' entries | `History.window(user, hidden_items)`: the worker passes the ids of other parked items, from `turns.inputs`. One more query, no join across slices |
| `attachments.tools` -> `db.conversations.current_id`, `entries` (recall) | `AttachmentTools` takes a `ConversationScope` protocol (`async visible_conversations(user) -> list[int]`), which `conversations` implements and `app` wires in |
| `memory.sweeper` -> `queue.QueueActivity` | `turns/background.py` owns the job runner; `archive` and `facts` only provide jobs |
| `tools.dates`, `agent.turn` -> `reminders.when` formatting | `times.py` (infra) |
| `inputs.loop` -> `reminders.loop.seconds_to_next_check` | `times.py` |
| `inputs.loop` -> `inputs.render` (Telegram HTML) | the loop calls a `QuestionEditor.ended(user, request, reason)` protocol; `telegram/questions.py` renders it |
| `config` -> `tools.catalog` | D4 |
| `memory.facts`, `memory.extract` -> `memory.summaries` (protocols, `load_transcript`) | `llm/protocols.py`, `conversations/transcript.py` |

### D6. Notices per feature

Each function moves to the package that sends it:

```
agent/notices.py              failed, unreachable, unreachable_mid, too_many_steps
turns/notices.py              fell_back
turns/inputs/notices.py       input_not_done
conversations/notices.py      reset_marker
reminders/notices.py          reminder
facts/notices.py              memory_list, memory_none, memory_forgot, memory_usage
attachments/notices.py        too_large, unreadable, not_kept
telegram/notices.py           text_only, unsupported
```

`tests/test_notices.py` is split the same way. The wording doesn't change.

### D7. Enforcement

- `import-linter` (dev dependency) with a `layers` contract that copies
  D1's table, and `exhaustive = true` so a new top-level package must be
  given a rank. `make lint` runs `lint-imports`.
- Imports under `TYPE_CHECKING` count. A type-only import still couples
  the code, and that's how `tools.base` -> `config` went unseen.
- `scripts/check_layout.py` keeps checks 1-4 (module and class order, `_`
  names) and drops check 5, which the contract covers.
- The contract goes into `make lint` in the last group, once the packages
  exist. Earlier groups each verify their own edges with a grep stated in
  the task.

### D8. Commits

Each group is a short series: a commit that only moves files (`git mv` and
import rewrites), listed in `.git-blame-ignore-revs`, then the commits that
change structure (D3-D6). Moves are reviewed with `git diff --color-moved`.
Tests move with their modules and mirror the package layout.

## Risks / Trade-offs

- [A huge diff hides a real change] → moves and structural edits are
  separate commits. No test changes what it asserts, and every group ends
  green.
- [The `[memory]`/`[ai]` multiple-inheritance sections fight pydantic
  (field order, validators)] → the example-config test checks every key.
  The fallback is a small explicit split in `config.py`, contained to two
  sections.
- [Moved out of `load_config`, the tool-name check could end up running
  after connections open, so a typo would fail later and less clearly] →
  it runs before `_build_services`. A test with an unreachable database URL
  shows the config error, not a connection error.
- [Passing hidden item ids instead of a join costs one query per turn] →
  it's one indexed lookup of the user's live requests, which already runs
  in the worker.
- [The feature order may need to change later] → it's one contract line,
  and a change that needs it says why.

## Migration Plan

Nothing to deploy differently. The image, config, schema and stored data
are unchanged. Rolling back is pinning the previous image.
