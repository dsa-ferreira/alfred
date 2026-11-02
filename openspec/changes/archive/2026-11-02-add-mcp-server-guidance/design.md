# Design

## Context

`build_turn_tools` (`src/alfred/mcp/registry.py`) opens a connection to every
configured server at the start of each turn, concurrently, and lists its tools.
The SDK v2 `Client` exposes the server's instructions as `client.instructions`
(`str | None`), populated once `async with Client(...)` has entered, which is
the same handshake the listing already does. alfred never reads it.

`system_prompt` (`src/alfred/agent/prompts.py`) builds the prompt in layers:
persona, operating rules, then this turn's additions (unavailable note, ask
rule). The model sees tools as `<server>_<tool>`, but a server's instructions
name its tools unprefixed ("call list_units").

## Goals / Non-Goals

**Goals:**
- Server instructions reach the model on every turn, at no extra network cost.
- The model can map tool names in the instructions to the names it was offered.
- The owner can mute or bound what a server puts into the prompt.

**Non-Goals:**
- Owner-written per-server hints (deferred; see proposal).
- On-demand guidance ("skills"): instructions are short, and an extra fetch step
  costs a model round-trip and is easy for a small model to skip.
- Reading MCP prompts or resources.

## Decisions

### D1. Read instructions during the tool listing

`_list_tools` returns the server's instructions with its tools (for example a
small `Listing(tools, instructions)`), and `TurnTools` gains
`instructions: list[ServerInstructions]` (server name and text), in
configuration order. Muting (`use_server_instructions = false`), blank-text
filtering and truncation happen there, so `TurnTools` holds exactly what the
model will see.

*Alternative:* a separate connection or a cache of instructions per server.
Rejected because the connection is already open, and a cache would serve stale
text after a server update, which the "no stale tool lists" choice already
avoids for tools.

### D2. Prompt placement and framing

A new section goes after the operating rules and before the unavailable note
and the ask rule, so the ask rule stays last. Format, one block per server:

```
Instructions from the tool server "cookbook". Its tools are offered to you
as cookbook_<tool>; where this text names a tool, use that prefixed name.

<instructions text>
```

The framing makes the prefix mapping explicit (otherwise a small model may
call `list_units` and get "unknown tool"). It also marks the text as the
server's, not the owner's.

*Alternative:* rewriting tool names inside the text to their prefixed form.
Rejected because it's fragile (names in prose, partial matches) and changes
text the server owns.

### D3. Config shape

- `McpServerEntry.use_server_instructions: bool = True`, carried on the
  resolved `McpServer`.
- `MCPConfig.instructions_max_chars: PositiveInt = 2000`.

Both are optional, so existing `alfred.toml` files stay valid. A global cap
(not per server) matches `tool_result_max_chars`. The default of 2000 fits
cookbook (~700) with room, and keeps a runaway server from flooding a 12B
model's context.

### D4. Truncation

The text is cut with the same shape as tool results (reusing `truncate`, which
appends a `[truncated: N more characters]` marker). A warning naming the server
and the original length is logged each turn it happens. That repeats, but it
points at a config or server fix.

### D5. Trust

Server text goes into the system prompt, so a server can steer the model.
alfred only talks to servers the owner configured and authenticates them with
the owner's tokens, so their instructions are treated like their tool
descriptions, which already reach the model. Mitigations are the per-server
mute and the length cap. Tool permissions are unchanged: excluded tools stay
refused even if the instructions mention them.

## Risks / Trade-offs

- [Instructions mention a tool alfred excludes (e.g. cookbook's
  `delete_recipe`)] → The call is refused as today and the model gets the
  "disabled in alfred's configuration" error. If it becomes noisy, mute or fix
  the server's text.
- [Prompt grows with every server] → Cap per server, mute per server. With
  three servers it stays at a few thousand characters at most.
- [A server's instructions are written for a strong model and confuse gemma] →
  Mute that server and fix its text upstream.

## Migration Plan

Additive: deploy 0.3.0 and the instructions start flowing with no config
change. Rollback means redeploying 0.2.0, or setting
`use_server_instructions = false` on a problematic server.
