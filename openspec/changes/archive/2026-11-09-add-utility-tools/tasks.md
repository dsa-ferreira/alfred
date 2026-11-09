# Tasks

## 1. Message times in the history

- [x] 1.1 Read `created_at` in `History.recent()` and add it to `HistoryEntry`; verify `tests/test_history.py` still passes and an entry returned by `recent()` has its `created_at`
- [x] 1.2 Add a stamp formatter (`[Mon 9 Nov 14:05]`, `ai.timezone`, absolute, no year) and prefix every `user`-role entry in `to_messages()` with it, before its label (design D1); verify with tests for owner, notice, ask_prompt, reminder and recall entries, for unstamped assistant replies, and that an entry renders identically when "now" moves to the next day
- [x] 1.3 Stamp the live `/ask` input in `agent/turn.py` with the turn's `now`; verify with a whole-turn test (`tests/agent/turn_helpers.py`) that the ask input starts with `[Tue 10 Nov 03:00] [ask:disk-check]`
- [x] 1.4 Remove a leading echoed stamp from the final answer before the `NO_REPLY` check, sending and recording (design D2); verify with turn tests for an echoed stamp, for `[...] NO_REPLY` being treated as no reply, and for `Saved [draft] for later.` being left unchanged
- [x] 1.5 Add the message-time paragraph to `OPERATING_RULES` and change the `[notice:]`/`[ask:]`/`[recall:]`/`[reminder:]` lines from "starting with" to "labelled" (design D6); update `tests/agent/test_prompts.py` and verify it passes with and without a custom persona

## 2. Calculator

- [x] 2.1 Add `pint` to `pyproject.toml` with `uv add pint`, and verify `make install` succeeds and `uv run python -c "import pint"` works
- [x] 2.2 Implement the whitelisted AST evaluator and result formatting in `tools/calculator.py` (design D3); verify with unit tests for every scenario under "Exact arithmetic", "Expressions are never executed as code" and "Calculation limits" in `specs/calculator/spec.md`, including that `9 ** 9 ** 9` returns at once
- [x] 2.3 Add unit conversion with a registry built once, unit-string checks and error mapping (design D4); verify with tests for the "Unit conversion" and "Unit errors" scenarios, `°F`, `floz`, `m^2`, the refused `m**99999999`, and an error when only one unit is given
- [x] 2.4 Write the `alfred_calculate` definition (description starting "Use for any arithmetic...", `%` is modulo, percentages as `/100`, unit names listed) and `CalculatorTools(LocalToolset)` offered on every turn; verify with toolset tests in `tests/tools/test_calculator.py` for owner, ask and reminder turns and for invalid JSON arguments

## 3. Date tool

- [x] 3.1 Add a public `parse_date` (`YYYY-MM-DD` or `today` against `now` in a zone) to `reminders/when.py`, reusing `_date` and `WhenError`; verify `tests/reminders/test_when.py` passes with new cases for `today` just after local midnight and for `2027-02-30`
- [x] 3.2 Implement `between`, `shift` (with month-end limiting and year-range errors), `weekday` and `now_in` (IANA zone, offset from the owner, owner's time when no zone) in `tools/dates.py` (design D5); verify with unit tests for every scenario in `specs/date-tools/spec.md`, plus a half-hour offset zone (`Asia/Kolkata`)
- [x] 3.3 Write the `alfred_date` definition (`op` enum, parameters, description) and `DateTools(zone, now=...)` offered on every turn, with errors for a missing or unknown op or a missing parameter; verify with toolset tests in `tests/tools/test_dates.py`

## 4. Wiring and rules

- [x] 4.1 Add `CalculatorTools` and `DateTools` to `build_toolsets` after `BrainTools` and before `McpTools`, passing the zone and clock (design D7); verify with a test that both are offered on owner, ask and reminder turns and come before MCP tools
- [x] 4.2 Add the calculation-routing paragraph to `OPERATING_RULES` (design D6); verify with `tests/agent/test_prompts.py` that it names both tools, with and without a custom persona
- [x] 4.3 Add whole-turn tests where the model calls `alfred_calculate` and then `alfred_date` and answers from the results; verify they pass with `make test`

## 5. Documentation

- [x] 5.1 Document the calculator, the date tool and the message times in `README.md` (a section beside "Web search and fetch" and "Reminders", and a note on the stamps where the history is described); verify the examples in it match the tests' outputs
- [x] 5.2 Run `make lint` and `make test` and verify both pass

## 6. Manual check on the real brains

- [x] 6.1 With `make run`, ask both brains (gemma 26B-A4B and bonsai 27B) "what's 347 times 28.5", "350 °F in Celsius", "how many days until Christmas", "what weekday is 3 March 2027", "what time is it" and "what time is it in Tokyo"; verify each answer comes from a tool call in the turn log and no reply starts with a stamp

## Workflow follow-up

- Bump the version (`build: bump version to ...`) and build and push the image with `make push`.
- No homelab change is needed beyond the new image. Write a handover update only if the release process asks for one.
- Archive the change with `/opsx:archive` after review.
