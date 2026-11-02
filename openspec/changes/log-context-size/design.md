# Design

## Context

A turn (`Agent.run` in `src/alfred/agent/turn.py`) builds the system prompt,
the tool definitions (`build_turn_tools` plus alfred's local `alfred_*` tools)
and the history messages once, then calls `ModelClient.complete` up to
`ai.max_requests_per_turn` times, appending tool calls and results in between.
The memory service (`Memory._make_summary`) uses the same client for summaries.

`ModelClient._parse` reads only `choices[0].message`. The OpenAI-compatible
reply also carries `usage: {prompt_tokens, completion_tokens, total_tokens}`,
which alfred currently drops.

`TurnTools.definitions` doesn't record which server a definition came from,
but `TurnTools.routes[name].server.name` does.

## Goals / Non-Goals

**Goals:**
- Exact token counts per model request, from the server.
- A per-turn breakdown that shows which part of the context, and which server,
  is large.

**Non-Goals:**
- Estimating tokens locally or adding a tokenizer dependency.
- Knowing the model's context window, warning near it, or trimming context.
- Storing sizes or counts in the database, or exposing them as metrics.
- Logging the size of tool results that grow within a turn. The per-request
  prompt token counts already show that growth.

## Decisions

### Token counts travel on `ModelReply`

Add an optional `usage: Usage | None = None` to `ModelReply`, where `Usage` is
a frozen dataclass with `prompt_tokens: int` and `completion_tokens: int`.
`_parse` fills it when `usage` is an object holding integer `prompt_tokens` and
`completion_tokens`. Otherwise it leaves `None`. A missing or malformed
`usage` never raises `ModelFailed`, because a reply without counts is still a
good reply.

The callers log the counts, not `ModelClient`. Only they know the turn id,
the request number or the conversation id. Logging in the client would mean
passing that context in, or printing lines that can't be linked to a turn.
The default `None` keeps the test fakes (`tests/helpers.py`) working
unchanged.

### Sizes in characters, measured as sent

- **Tool definition:** `len(json.dumps(definition, ensure_ascii=False))`. That
  is the JSON the model server receives, schema included.
- **System prompt:** `len(content)`. Each server's instruction size is
  `len(text)` of the `ServerInstructions` text after the cut to
  `mcp.instructions_max_chars`. It is shown inside the system prompt figure,
  not added on top of it.
- **History:** the entry count and the JSON size of the messages after the
  system prompt at the start of the turn. For an `/ask` turn this includes the
  labelled ask input.

Characters, not estimated tokens. The ratio of characters to tokens depends on
the model's tokenizer and on the content (JSON schemas tokenize differently
from prose). A `chars/4` figure would look exact without being exact. The
exact total is already on the request line.

Tool definitions are grouped by `routes[name].server.name`. Definitions with
no route are alfred's local tools, grouped as `alfred`.

### Log line shapes

Plain integers, so the lines are easy to grep and compare:

```
turn 812 context: system 5214 chars (instructions: cookbook 640, mcparr 412); tools 41 defs 28410 chars (cookbook 9812, mcparr 6204, alfred 2930); history 30 entries 11032 chars
turn 812 request 1: 6812 prompt tokens, 38 completion tokens
turn 812 request 2: token counts not reported
conversation 41 summary: 3120 prompt tokens, 58 completion tokens
```

The context line is logged once per turn, after the messages are built and
before the first request. At DEBUG, a second line lists the five largest
definitions: `turn 812 largest tools: cookbook_update_recipe 2410, ...`. It is
computed only when `log.isEnabledFor(logging.DEBUG)`.

The figures are computed in one helper in `turn.py`, which returns the line's
text. That keeps `Agent.run` readable and lets tests check the line without
parsing logs.

## Risks / Trade-offs

- [Ollama may report only newly processed prompt tokens when it reuses its
  KV cache, so a later request in a turn can show fewer prompt tokens than the
  one before it.] → The README notes that `prompt_tokens` is whatever the
  server reports. The character breakdown doesn't depend on the server.
- [Serialising every tool definition each turn costs a little CPU.] → It's a
  few dozen small `json.dumps` calls next to a model request that takes
  seconds. That cost is negligible.
- [Log volume grows by 1 + N lines per turn.] → They're single short lines at
  INFO, and the per-tool detail is only at DEBUG.
