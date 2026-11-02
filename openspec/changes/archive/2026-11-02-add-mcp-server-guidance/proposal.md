# Proposal

## Why

MCP servers can send `instructions` at connect time: how their tools fit
together and when to use them. cookbook and mcparr already send them (for
example, "units come from a fixed vocabulary: call list_units"), and the
upcoming Trello server will use them to say what its board column is for and
that cards can't be archived. alfred receives that text on every turn and
throws it away, so the model calls tools without the guidance their own
servers wrote for it.

## What Changes

- Every turn, alfred reads each available MCP server's instructions from the
  connection it already opens to list tools, and adds them to the system
  prompt after the operating rules, one block per server, naming the
  server's tool prefix.
- Servers that are unavailable for the turn, or send no instructions,
  contribute nothing.
- New per-server setting `use_server_instructions` (default `true`) to mute a
  server's instructions without editing that server.
- New setting `[mcp] instructions_max_chars` (default 2000) caps each
  server's instructions; longer text is cut with a marker and a warning is
  logged.
- No owner-written per-server hints. Servers we control say what they are for
  through their own instructions. Hints can be added later for third-party
  servers.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mcp-tools`: new requirements for forwarding server instructions, muting
  them per server, and capping their length.
- `agent-loop`: the system prompt requirement gains the server-instructions
  section between the operating rules and the turn's additions.

## Impact

- Code: `src/alfred/mcp/registry.py` (collect instructions while listing),
  `src/alfred/agent/prompts.py` (new prompt section), `src/alfred/agent/turn.py`
  (wiring), `src/alfred/config.py` (two new optional settings).
- Config: `alfred.toml.example` documents the new optional keys. Existing
  config files keep working (no breaking change).
- Model prompt: longer by the size of the configured servers' instructions
  (today cookbook's is ~700 characters).
- No new dependencies; uses `mcp.Client.instructions` from the SDK v2.
- Deployment: a minor release (0.3.0) and a handover update listing the new
  optional keys.
