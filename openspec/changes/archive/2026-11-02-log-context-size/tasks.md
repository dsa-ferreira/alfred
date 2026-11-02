# Tasks

## 1. Token counts from the model server

- [x] 1.1 Add a frozen `Usage` dataclass (`prompt_tokens`, `completion_tokens`) and an optional `usage: Usage | None = None` field on `ModelReply` in `src/alfred/agent/model.py`. Verify `make test` still passes unchanged (the fakes in `tests/helpers.py` don't set it).
- [x] 1.2 Make `_parse` fill `usage` from the reply's `usage` object when both counts are integers, and leave `None` when it is missing or malformed, never raising. Verify with new tests in `tests/agent/test_model.py` for a reply with usage, one without, and one with malformed usage (still a valid reply).

## 2. Turn logging

- [x] 2.1 Add a helper in `src/alfred/agent/turn.py` that builds the turn's context line (system prompt size with per-server instruction sizes, tool definition count and total, sizes per server and for `alfred`, history entry count and size, all in characters per design.md) and log it once per turn at INFO before the first model request. Verify with a test in `tests/agent/test_turn.py` that uses two fake servers and checks the line's figures. Also test that an unavailable server is absent from the line, and that no history or prompt text appears in it.
- [x] 2.2 At DEBUG only, log the turn's five largest tool definitions by model-facing name and size, largest first. Verify with tests that the line appears under `caplog.at_level(logging.DEBUG)` with the right order and is absent at INFO.
- [x] 2.3 After each model request in a turn, log `turn <id> request <n>: <p> prompt tokens, <c> completion tokens`, or `token counts not reported` when `usage` is `None`. Verify with a test where a two-request turn logs both lines with their counts, and a test where a reply without usage still produces the normal answer.

## 3. Summary logging

- [x] 3.1 In `Memory._make_summary` (`src/alfred/memory/service.py`), after a successful summary request, log `conversation <id> summary: <p> prompt tokens, <c> completion tokens` or `token counts not reported`. Verify with a memory service test that checks the line and that the title and summary are still stored when usage is missing.

## 4. Documentation

- [x] 4.1 Add a short "Context size" note to `README.md` (next to the MCP server sections) that explains the context line, the per-request token lines, the DEBUG tool list, and the caveat that a server reusing its prompt cache (such as Ollama) may report only newly processed prompt tokens. Verify that the example lines in the README match the format the tests assert.
- [x] 4.2 Run `make lint` and `make test`, and verify both pass.
