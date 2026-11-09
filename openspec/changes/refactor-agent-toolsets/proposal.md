# Proposal

## Why

Every local tool feature (conversation reset, memory, brains, web, reminders)
has been wired into `agent/turn.py` by name: its tool schemas, a `Protocol`, a
constructor argument, an `if` that decides when it is offered and a second
`elif` that dispatches its calls, plus an identical try/except. Adding a
feature now means touching five places in that file, `app.py` and often the
worker, and the turn loop itself is buried under the wiring. Small helpers in
the wrong modules (`ToolOutcome` in `mcp/`, `backoff_s` in `queue`) tangle the
import graph further. This change makes the next feature cheap to add.

## What Changes

- One toolset interface for everything the model can call. A toolset is
  opened once per turn and decides for itself whether it is offered (owner
  turns only, more than one brain, ...), keeps its own per-turn state, and
  handles its own calls. The turn loop no longer names any feature.
- Conversation reset (`alfred_end_conversation`), brain switching
  (`alfred_set_model`), memory search and recall, reminders, web search and
  fetch, and the MCP servers each become a toolset. Their tool schemas move
  out of `agent/turn.py` to the feature that owns them.
- One shared wrapper turns an unexpected exception in a tool into an error
  result for the model, replacing the three copies in `agent/turn.py`.
- What a toolset adds to the system prompt (today only the MCP server
  instructions and the unavailable-servers note) arrives as generic prompt
  notes, so the system prompt no longer has MCP-specific parameters.
- `ToolOutcome` and `parse_arguments` move from `mcp/registry.py` into the
  toolset contract; `backoff_s` moves out of `queue.py`, so memory no longer
  imports the queue.
- The toolset contract (the interfaces, `ToolOutcome`, `TurnInput`) lives in
  a new `alfred.tools` package, together with alfred's own tools that belong
  to no feature package (conversation reset, brain switching). The agent loop
  and every tool then depend on the contract; no tool depends on the agent
  package for it.
- `memory/service.py` splits into summaries, the sweeper, and search/recall.
- `tests/agent/test_turn.py` splits by toolset.
- `app.py` builds the list of toolsets and passes it to the agent.

No behaviour changes: the same tools are offered on the same turns in the same
order, calls produce the same results, the system prompt and the context-size
log line are byte-for-byte the same. Existing tests keep their assertions;
only their setup and imports adapt.

Out of scope: bringing together the per-kind (owner / ask / reminder) branches
spread over the worker and the agent.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This is a pure refactor; the change sets `skip_specs: true`.

## Impact

- Code: `src/alfred/agent/turn.py` (shrinks to the loop and brain fallback),
  a new `src/alfred/tools/` package (`base.py`, `conversation.py`,
  `brains.py`), new `agent/context.py`, `agent/prompts.py`, `agent/brains.py`,
  `app.py`, `queue.py`, `mcp/registry.py` plus a new MCP toolset,
  `web/tools.py`, `reminders/tools.py`, `memory/service.py` (split).
- Tests: `tests/agent/test_turn.py` split; setup in memory, queue, prompt and
  registry tests adapts to the new constructors and module paths.
- No change to configuration, the database, the HTTP API, Telegram, or the
  container image. No deploy steps.
