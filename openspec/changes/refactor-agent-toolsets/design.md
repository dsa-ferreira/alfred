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
    group: str                                   # context-size log group
    prompt_notes: list[PromptNote]
    def new_attempt(self, attempt: AttemptContext) -> None
    def handles(self, name: str) -> bool
    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome
```

`open` runs once per turn in `Agent.run`, in the order `app.py` gives:
web, conversation, memory, reminders, brains, MCP. The concatenated
`definitions` are therefore in today's order. Gating ("owner turns only",
"more than one brain") moves into each toolset's `open`, which returns `None`
when it is not offered.

A base class `LocalToolset` supplies `group = "alfred"`, empty
`prompt_notes`, and a no-op `new_attempt`, so most toolsets implement only
`open`, `handles` and `call`.

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
each toolset's `group` (the MCP toolset reports per-server groups through a
`group_of(name)` override), groups in definition order except `alfred`,
which is listed last as today. `largest_tools` moves with it unchanged.

### D7. Where things live

```
agent/toolset.py      Toolset, OpenToolset, LocalToolset, AttemptContext,
                      PromptNote, ToolOutcome, parse_arguments, safe_call
agent/turn.py         Agent (loop, fallback, input messages, _record),
                      TurnInput, TurnResult, TurnFailed
agent/context.py      context_sizes, largest_tools
agent/conversation.py ConversationTools (alfred_end_conversation)
agent/brains.py       Brains + BrainTools (alfred_set_model)
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
  imports, fixtures and constructor calls; review checks this.
- The number of collected tests does not go down (recorded before step 1,
  compared at the end).
- New unit tests cover only the new seams: dispatch order, the unknown-tool
  fallback, `safe_call`, and `new_attempt` being called each attempt.

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
