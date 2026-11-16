# Tasks

Every group ends with `make lint` and `make test` green. Worker, parking
and input-request tests keep their assertions; only how they build things
changes.

## 1. Kinds package

- [x] 1.1 Create `turns/kinds/` with the `TurnKind` protocol, `Opening`, `Ending`, `NewEntry` and the `Kinds` registry (design D1, D2); verify the registry finds each kind by tag and by turn type in a test
- [x] 1.2 Write `ChatKind`, `AskKind` and `ReminderKind` with their payloads moved in from `turns/payloads.py` (same JSON), alongside the old `kinds.py`; verify table-driven tests with no fakes cover every cell of design's Context table (answer, failure ending, entries, message, title, opening), and the payload round-trip tests pass from their new place
- [x] 1.3 Make `Queue.add` take the tag from the registry, and delete `turns/payloads.py`; verify the queue tests pass

## 2. Framing

- [x] 2.1 Add `agent/framing.py` (design D4), implement `framing` on each kind, and make `Agent.run` take a `Framing`; verify the prompt tests produce the same system prompt and input line for each kind
- [x] 2.2 Remove `prompt_flags`, `input_line`, `_PromptFlags` and `_TimeFormat` from `kernel/turn.py`; verify the turn classes hold only data and `kernel/` imports nothing new

## 3. Worker and exchange

- [x] 3.1 Add `turns/exchange.py` (design D3) and make the worker use the kinds' decisions for starting, completing and failing; verify the worker tests pass unchanged, plus one test per `Ending` that checks the finish order against record and send
- [x] 3.2 Delete the old `kinds.py` (`Kind`, the `finish` callback and the three handlers); verify `grep -rn "finish: Callable\|class .*Kind:" src/alfred/turns` finds only `turns/kinds/`

## 4. Parked turns and unanswered questions

- [x] 4.1 Take parking's question title from `kind.question_title(turn)`, and delete `turns/inputs/titles.py`; verify the parking tests show the same titles
- [x] 4.2 Make `InputRequests` return ended rows without recording, and add `turns/inputs/endings.py`, called from the cancel and expiry paths, to record them through their kinds (design D5); verify the input-request tests record the same entries, and `turns/inputs/store.py` imports neither the conversation store nor notices

## 5. Check and docs

- [x] 5.1 Verify no module outside `turns/kinds/` and toolsets' `offered`/`open` branches on kind: `grep -rnE "isinstance\([^)]*(Chat|Ask|Reminder)Turn|\.kind ==|match .*kind" src/alfred` lists only those (design D6)
- [x] 5.2 Describe turn kinds in AGENTS.md (what a `TurnKind` decides, `Exchange`, the delivery rule, and what adding a kind takes), and update the "Adding a tool group" sentence about `ChatTurn`/`AskTurn`/`ReminderTurn`; verify every path it names exists
