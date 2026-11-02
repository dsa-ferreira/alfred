# Proposal

## Why

Every MCP server added to alfred adds its tool definitions (and possibly its
instructions) to every model request, and nothing tells the owner how large
the requests have become. The model server already reports exact token counts
in each reply, but alfred discards them. The owner wants to watch the context
size in the logs while adding tools.

## What Changes

- Every model request logs the token counts the model server reports (prompt
  and completion tokens). This covers turn requests and conversation-summary
  requests. When the server reports no counts, the line says so.
- Every turn logs, once, how big the context it sends is and where the size
  comes from: the system prompt (with each server's instructions), the tool
  definitions (total and per MCP server, plus alfred's own tools) and the
  history. Sizes are in characters.
- At DEBUG level, the turn also logs its largest tool definitions, to find a
  single bloated schema.
- No new configuration, no context-length setting and no warnings: these are
  plain numbers for the owner to read.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-loop`: turns log the size of their context and the token counts of
  each model request.
- `conversation-memory`: summary requests log the token counts the model
  server reports.

## Impact

- `src/alfred/agent/model.py`: read `usage` from the chat completions reply.
- `src/alfred/agent/turn.py`: the per-turn breakdown line and per-request
  token lines.
- `src/alfred/memory/service.py`: token line for summary requests.
- Tests for the model client, the turn and the memory service; README
  (logging notes).
- No API, database, configuration or dependency changes.
