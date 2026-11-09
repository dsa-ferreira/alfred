# Tasks

Baseline before any code change: `uv run pytest --collect-only -q` collects
708 tests. Existing test assertions must not change (design D9); only
imports, fixtures and constructor calls may.

## 1. Pin today's behaviour

- [x] 1.1 Add a test in `tests/agent/test_turn.py` that asserts the full ordered list of tool names offered on an owner turn with web, memory, reminders, two brains and both MCP test servers, and one for an ask turn; verify both pass on the current code with `make test`

## 2. Move shared helpers

- [x] 2.1 Create `src/alfred/agent/toolset.py` holding `ToolOutcome` and `parse_arguments` (moved from `mcp/registry.py`); update every importer in `src/` and `tests/`; verify `make lint` and `make test` pass and `grep -rn "registry import.*ToolOutcome\|registry import.*parse_arguments" src tests` finds nothing
- [x] 2.2 Move `backoff_s` from `queue.py` to `src/alfred/backoff.py` with the same signature; update `queue.py`, `memory/service.py` and tests; verify `grep -rn "from alfred.queue import backoff_s" src tests` finds nothing and `make test` passes

## 3. Toolset interface

- [x] 3.1 Add `Toolset`, `OpenToolset`, `LocalToolset` (group `alfred`, no notes, no-op `new_attempt`), `AttemptContext` (turn, owner texts, `request_reset`) and `PromptNote` to `agent/toolset.py` as in design D1, D2, D5; verify `make lint` passes
- [x] 3.2 Add `safe_call` (design D4) and a `dispatch` helper (first open toolset that handles the name, else `Error: unknown tool {name!r}.`, design D3) with unit tests in `tests/agent/test_toolset.py` for dispatch order, the unknown-tool text and an exception becoming `Error: tool 'x' failed: ValueError`; verify the new tests pass

## 4. Features become toolsets

Each class keeps working with today's `Agent` until group 6 switches it over.

- [x] 4.1 Add `ConversationTools` in `src/alfred/agent/conversation.py` (owns `END_CONVERSATION_TOOL`, opens on owner turns only, calls `request_reset`); verify unit tests for gating and the reply text pass
- [x] 4.2 Add `BrainTools` in `src/alfred/agent/brains.py` (owns `set_model_tool`, opens on owner turns with more than one brain, the switch logic from `Agent._set_model`); verify unit tests for gating, unknown brain and already-current pass
- [x] 4.3 Make `ReminderTools` implement `Toolset` (owner turns only); verify `tests/reminders/test_tools.py` passes and a gating test is added
- [x] 4.4 Make `WebTools` implement `Toolset`: opens on every turn, `new_attempt` builds the `WebTurn` allowlist from the context's owner texts plus the `/ask` prompt; verify `tests/web/test_tools.py` passes with setup-only changes
- [x] 4.5 Add `McpTools` in `src/alfred/mcp/toolset.py`: `open` calls `build_turn_tools`, `handles` covers routes and exclusions, `call` uses `call_tool` with the configured timeout and `tool_result_max_chars`, per-server groups, prompt notes for instructions (with `log_label`/`log_chars`) and the unavailable note; move `server_instructions` and `unavailable_note` here; verify `tests/mcp/test_registry.py` still passes and new tests cover `handles` and the notes

## 5. Memory split and memory toolset

- [x] 5.1 Split `memory/service.py` into `memory/summaries.py` (`Summaries`), `memory/sweeper.py` (`Sweeper`) and `memory/search.py` (`ConversationSearch`) per design D8; update `queue.Worker` to take `Summaries`; verify `tests/memory/` and `tests/test_queue.py` pass with setup-only changes
- [x] 5.2 Add `MemoryTools` in `memory/search.py` (owns the search and recall schemas, owner turns only, argument handling from `Agent._memory_tool`); verify unit tests for gating and invalid JSON pass

## 6. Agent switches to toolsets

- [x] 6.1 Change `system_prompt` to take `notes: Sequence[str]` in place of `instructions`/`unavailable` (design D5); update `tests/agent/test_prompts.py` calls to pass the rendered notes; verify every prompt test passes with unchanged expected text and `agent/prompts.py` no longer imports `alfred.mcp`
- [x] 6.2 Move `context_sizes` and `largest_tools` to `src/alfred/agent/context.py`, taking the open toolsets (design D6); verify the context-size and largest-tools tests pass unchanged
- [x] 6.3 Rewrite `Agent` as `Agent(config, model, history, sessions, *, brains, toolsets, now)`: open toolsets once per turn, `AttemptContext` and `new_attempt` per attempt, dispatch through `safe_call`, reset read from the context; remove the tool schemas, Protocols and `_*_tool` methods from `agent/turn.py`; verify `agent/turn.py` imports nothing from `alfred.web`, `alfred.reminders`, `alfred.memory` or `alfred.mcp` (except via `agent.toolset`)
- [x] 6.4 Update the `agent()` helper and the direct `Agent(...)` calls in `tests/agent/test_turn.py` to build toolsets; verify all of `tests/agent/test_turn.py`, including the tests from 1.1, passes with no assertion changed (`git diff` shows only setup)
- [x] 6.5 Update `app.py` to build the toolsets in the order web, conversation, memory, reminders, brains, MCP, run the `Sweeper`, and close clients as before; verify `tests/test_serve.py` passes

## 7. Split the turn tests

- [x] 7.1 In a commit of its own, move tests verbatim out of `tests/agent/test_turn.py` by toolset: end-conversation and set-model to `tests/agent/test_conversation.py` and `tests/agent/test_brain_tools.py`, memory to `tests/memory/test_in_turn.py`, web to `tests/web/test_in_turn.py`, reminders to `tests/reminders/test_in_turn.py`, context-size to `tests/agent/test_context.py`; move the shared `agent()` helper to `tests/agent/turn_helpers.py`; verify `make test` passes and the collected count equals the count before the move

## 8. Docs and final check

- [x] 8.1 Update the module docstrings of `agent/turn.py`, `agent/toolset.py` and `agent/prompts.py` to the new structure, and add a short "Adding a tool group" note to `AGENTS.md` (implement `Toolset`, add it to the list in `app.py`); verify the note matches the code
- [x] 8.2 Run `make lint` and `make test`; verify both pass and `uv run pytest --collect-only -q` collects at least 708 tests plus the ones added in this change

## 9. Separate the toolset contract from the agent (one commit, design D10)

- [x] 9.1 Create `src/alfred/tools/` with `base.py` holding everything from `agent/toolset.py` plus `TurnInput` (moved from `agent/turn.py`); delete `agent/toolset.py`; update every importer in `src/` and `tests/`; verify `make test` passes and `grep -rn "alfred.agent.toolset" src tests` finds nothing
- [x] 9.2 Move `ConversationTools` to `src/alfred/tools/conversation.py` and `BrainTools`, `SET_MODEL` and `set_model_tool` to `src/alfred/tools/brains.py` (`Brains` stays in `agent/brains.py`); update `app.py`; move their unit tests and the set-model turn tests to `tests/tools/`; verify `make test` passes and `uv run pytest --collect-only -q` still collects 731 tests
- [x] 9.3 Verify the dependency direction: `grep -rn "from alfred.agent" src/alfred/tools src/alfred/web src/alfred/reminders src/alfred/memory src/alfred/mcp` shows only `alfred.agent.brains` (in `tools/brains.py`) and `alfred.agent.model` (in `memory/summaries.py`)
- [x] 9.4 Update the module docstrings and the AGENTS.md "Adding a tool group" note to `alfred.tools.base`; run `make lint` and `make test`; commit following the commit-messages skill; verify the note matches the code and both checks pass
