# Tasks

## 1. Configuration

- [x] 1.1 In `src/alfred/config.py`, add `use_server_instructions: bool = True` to `McpServerEntry` and the resolved `McpServer`, and `instructions_max_chars: PositiveInt = 2000` to `MCPConfig` (design D3); in `tests/config/test_config.py` check the defaults, custom values, and that `instructions_max_chars = 0` is rejected; verify with `make test`
- [x] 1.2 Document both keys in `alfred.toml.example` (the cap under `[mcp]`, `use_server_instructions = true` on one server entry, with a one-line comment each); verify `tests/config/test_example.py` passes

## 2. Collect instructions per turn

- [x] 2.1 In `src/alfred/mcp/registry.py`, have `_list_tools` also return `client.instructions` (checked against the SDK v2 Client docs: `str | None`, set inside `async with`), and add `instructions` (server name + text, configuration order) to `TurnTools`, skipping unavailable, muted and blank ones and truncating with `truncate` plus a warning naming the server (design D1, D4); verify with `make test`
- [x] 2.2 Give `make_cookbook()` in `tests/mcp/servers.py` an `instructions=` text, and add tests in `tests/mcp/test_registry.py`: instructions collected for an available server; none when the server is down; none for a server without instructions or with blank ones; none when muted while its tools are still offered; truncation with marker and a logged warning naming the server; changed instructions picked up on the next turn; verify with `make test`

## 3. System prompt

- [x] 3.1 In `src/alfred/agent/prompts.py`, add the per-server instructions section framed as in design D2 (server name, `<server>_<tool>` prefix note) and give `system_prompt` an `instructions` argument, placed after the operating rules and before the unavailable note and the ask rule; extend `tests/agent/test_prompts.py`: order persona → rules → instructions → `NO_REPLY` on an ask turn, the prefix note present, a custom persona keeps the instructions, no section when the list is empty; verify with `make test`
- [x] 3.2 Pass `tools.instructions` to `system_prompt` in `src/alfred/agent/turn.py`; add a test in `tests/agent/test_turn.py` that the system message the model receives contains cookbook's instructions; verify with `make test`

## 4. Integration and release

- [x] 4.1 Run `make lint` and `make test`; both pass
- [x] 4.2 Manual check against the real model: `make run`, ask alfred to add a short recipe with US units and confirm it calls `cookbook_list_units` and uses the catalogue's unit codes; confirm the log shows no truncation warning for cookbook or mcparr
- [x] 4.3 Bump the version to 0.3.0 in `pyproject.toml`; verify `make image` tags `alfred:0.3.0`
- [x] 4.4 Write the handover update with the `homelab-handover` skill: new optional keys `[mcp] instructions_max_chars` and `[[mcp_servers]] use_server_instructions`, no required action; verify the update file exists under `handover/updates/`

## Workflow follow-up

- Archive the change with `/opsx:archive` once implemented and deployed.
