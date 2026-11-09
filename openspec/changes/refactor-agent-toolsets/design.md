# Design

## Context

See proposal.md for why. What shapes the approach today:

- `Agent.run` lists the MCP servers once per turn (`build_turn_tools`), then
  builds the local tool list with feature-specific `if`s: web on every turn;
  end-conversation, memory, reminders and set-model only on owner turns
  (set-model only with more than one brain). Local definitions come first, in
  the order web, end-conversation, memory search + recall, reminders,
  set-model, followed by the MCP tools.
- `Agent._attempt` runs once per brain attempt (a second time only after a
  brain fallback with no tool run). Each attempt reads the history again,
  creates a fresh web allowlist seeded from the owner's entries in the
  window (plus the `/ask` prompt), and keeps its own `reset` flag.
- `Agent._run_tool` dispatches with an `elif` chain (end-conversation,
  memory, reminders, set-model, web, else MCP). Memory, reminder and web
  calls are wrapped in identical try/except blocks; MCP's `call_tool` never
  raises and truncates its own results; web results are deliberately not
  truncated.
- `system_prompt` takes MCP's `instructions` and `unavailable` explicitly and
  renders them after the brain line and before the ask/reminder rule.
- `context_sizes` logs instruction sizes per server (raw text length) and
  tool definition sizes grouped by MCP server, with all local tools in one
  `alfred` group listed last.

## Goals / Non-Goals

**Goals:**

- `agent/turn.py` contains the loop, brain fallback, the per-kind input
  messages and tool-call recording, and imports no feature module.
- A new local tool group is one module implementing the toolset interface
  plus one entry in the list `app.py` builds.
- Identical observable behaviour, including log lines the tests check.
- Tool code (`alfred.tools`, `web`, `reminders`, `memory`, `mcp`) imports the
  toolset contract from `alfred.tools.base`, never from `alfred.agent`. The
  only exceptions are the state and clients the tools act on:
  `agent.brains.Brains`, and `agent.model` for summaries.

**Non-Goals:**

- Moving per-kind (owner / ask / reminder) logic out of `Worker` or `Agent`.
- Changing which tools are offered when, or any tool's text.
- Making toolsets pluggable from configuration.

## Decisions

### D1. Two-level interface: `Toolset` opens an `OpenToolset` per turn

```
class Toolset(Protocol):
    async def open(self, turn: TurnInput) -> OpenToolset | None   # None: not offered

class OpenToolset(Protocol):
    definitions: list[dict[str, Any]]
    prompt_notes: Sequence[PromptNote]
    def group_of(self, name: str) -> str         # context-size log group
    def new_attempt(self, attempt: AttemptContext) -> None
    def handles(self, name: str) -> bool
    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome
```

`open` runs once per turn in `Agent.run`, in the order `app.py` gives:
web, conversation, memory, reminders, brains, MCP. The concatenated
`definitions` are therefore in today's order. Gating ("owner turns only",
"more than one brain") moves into each toolset's `open`, which returns `None`
when it is not offered.

A base class `LocalToolset` supplies `group_of` returning `"alfred"`, empty
`prompt_notes`, a no-op `new_attempt`, `handles` from its definitions, and
an `open` that returns itself when `offered(turn)` is true, so most toolsets
implement only `call` (and `offered` if they are gated).

*Alternative considered:* opening per attempt (simpler, no `new_attempt`).
Rejected because it would list the MCP servers again after a brain
fallback, which is a behaviour change.

### D2. Per-attempt state goes through `AttemptContext`

`AttemptContext` is created at the start of each `_attempt` and holds what
used to live on `_State` that toolsets need: the turn input, the owner texts
from that attempt's history window, and `request_reset()`. The agent calls
`new_attempt(ctx)` on every open toolset before the first model request.
Only the web toolset uses it: it builds the attempt's allowlist from the
owner texts plus the `/ask` prompt, exactly as today. `reset` is read from
the context when the attempt returns, so it stays per attempt.

### D3. Dispatch: first open toolset that handles the name

For each call, the agent asks the open toolsets in order and calls the
first whose `handles(name)` is true, through `safe_call` (D4). If none
handles it the result is `Error: unknown tool {name!r}.`, the text
`call_tool` gives today.

This reproduces today's results:
- A gated tool called when it was not offered (for example
  `alfred_end_conversation` on an ask turn) is handled by nobody local, so it
  reaches the MCP toolset or the fallback, as it reaches `call_tool` today.
- The MCP toolset handles names in its routes and in its configured
  exclusions, so the "disabled in alfred's configuration" message is kept.
- Local toolsets come first, so a name clash with an MCP tool (a server
  called `web` with a tool `search`) resolves to the local tool, as today.

### D4. One `safe_call`

`safe_call(open_toolset, name, raw_arguments, ctx)` catches any exception,
logs it with `log.exception("tool %s failed", name)` and returns
`ToolOutcome(False, f"Error: tool {name!r} failed: {type(exc).__name__}")`,
the text the three copies give today. It applies to every toolset. For
end-conversation, set-model and MCP, which never raise today, the only
difference is that a future bug becomes an error result, not a failed turn.
Truncation stays inside each toolset (MCP truncates, web does not).

### D5. Prompt notes replace MCP parameters

`PromptNote(text, log_label: str | None = None, log_chars: int = 0)`.
`system_prompt(..., notes: Sequence[str])` places the notes where server
instructions and the unavailable note go today. The MCP toolset produces one
note per server with instructions (framed by `server_instructions`, with
`log_label` = server name and `log_chars` = raw instruction length), then the
unavailable note if any server is down. `server_instructions`,
`unavailable_note` and `ServerInstructions` move to the MCP side;
`agent/prompts.py` no longer imports `mcp`.

### D6. Context-size log keeps its shape

`agent/context.py` takes the open toolsets and the start-of-turn messages.
Instructions come from notes with a `log_label`. Tool sizes are grouped by
each toolset's `group_of(name)` (the MCP toolset reports the tool's server),
groups in definition order except `alfred`, which is listed last as today. `largest_tools` moves with it unchanged.

### D7. Where things live

```
tools/base.py         Toolset, OpenToolset, LocalToolset, AttemptContext,
                      PromptNote, ToolOutcome, TurnInput, parse_arguments,
                      owner_only, dispatch, safe_call
tools/conversation.py ConversationTools (alfred_end_conversation)
tools/brains.py       BrainTools (alfred_set_model), over agent.brains.Brains
agent/turn.py         Agent (loop, fallback, input messages, _record),
                      TurnResult, TurnFailed
agent/context.py      context_sizes, largest_tools
agent/brains.py       Brains (state only)
memory/search.py      search and recall + MemoryTools
reminders/tools.py    ReminderTools implements Toolset (store stays public)
web/tools.py          WebTools implements Toolset; WebTurn becomes its attempt state
mcp/registry.py       unchanged functions, minus ToolOutcome/parse_arguments
mcp/toolset.py        McpTools: open() = build_turn_tools, call = call_tool
backoff.py            backoff_s (same signature, from queue.py)
```

`Agent(config, model, history, sessions, *, brains, toolsets, now)`. The
agent still needs `brains` for the fallback and the brain line; the same
`Brains` instance is given to `BrainTools`.

Module names `agent/turn.py` and `reminders/tools.py` are kept so the worker
and existing imports keep working.

### D8. Memory split

```
memory/summaries.py   Summaries: summarise, per-conversation locks,
                      `changed` event, retry bookkeeping, transcript loading
memory/sweeper.py     Sweeper(summaries): run_forever, sweep_once
memory/search.py      ConversationSearch: search, recall; MemoryTools toolset
```

The worker gets `Summaries` (it only calls `summarise`); `app.py` runs the
sweeper and gives `MemoryTools` to the agent. Constants move with the code
that uses them.

### D9. How "no behaviour change" is checked

- Existing test assertions do not change. Diffs to existing tests touch only
  imports, fixtures and constructor calls; review checks this. Two kinds of
  exception, both about structure rather than behaviour: the `serve` wiring
  tests find the web, memory and reminder objects inside the agent's
  toolsets instead of its keyword arguments, and a log filter follows the
  logger name `alfred.memory.service` to `alfred.memory.summaries`.
- The number of collected tests does not go down (recorded before step 1,
  compared at the end).
- New unit tests cover only the new seams: dispatch order, the unknown-tool
  fallback, `safe_call`, and `new_attempt` being called each attempt.

### D10. The contract lives outside the agent package

Decided after groups 1-8 were implemented. With the contract in
`agent/toolset.py`, every feature package imported `alfred.agent`, so the
dependency pointed from the tools to the loop. The contract moves to
`alfred/tools/base.py`; the agent and all tools depend on it instead:

```
agent/  -->  tools/base.py  <--  tools/*, web, reminders, memory, mcp
```

`TurnInput` moves with it, because every toolset's `open` receives one;
`TurnResult` and `TurnFailed` stay in `agent/turn.py`, since only the agent
and the worker use them. The two toolsets that belong to no feature package,
`ConversationTools` and `BrainTools`, move to `alfred/tools/`. `Brains` stays
in `agent/brains.py`: the fallback needs it, and `tools/brains.py` may
depend on that state class while `tools/base.py` depends on nothing in
`agent/`.

*Alternatives considered:* moving only the two toolsets out of `agent/`
(the contract would still live in the agent package, so features would keep
importing it); moving every feature's toolset into `tools/` (rejected: it
splits each feature across two packages, e.g. web's allowlist away from the
tools that use it).

## Risks / Trade-offs

- [A subtle ordering change in offered tools or in the log line] → the
  existing tests check tool sets per turn kind and the exact context-size
  line; add one test that asserts the full ordered list of tool names on an
  owner turn before refactoring, so the order is pinned.
- [Large test diff hides an accidentally changed assertion] → split
  `test_turn.py` in its own commit, moving tests verbatim, separate from the
  commits that change setup.
- [`safe_call` now wraps tools that never raised] → only affects unexpected
  exceptions; accepted.
- [An extra abstraction for six toolsets] → the base class keeps each one
  small; the loop loses more code than the interface adds.

## Migration Plan

None. No configuration, schema or API change; deploy as a normal release.
Rollback is reverting the commits.
