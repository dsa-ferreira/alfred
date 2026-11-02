# Tasks

## 1. Configuration

- [x] 1.1 Add `models` (`[ai.models]`, alias -> tag) to `AIConfig` in `config.py`: aliases use the `Name` rule, tags are non-empty, duplicate tags and alias/brain-name clashes are errors naming `ai.models`; cover each scenario of "Brain list validation" and "Brains default to none" in `tests/config/test_config.py`
- [x] 1.2 Document `[ai.models]` in `alfred.toml.example` (commented example with `gemma` and `bonsai`) and add an `ai.models` row to the README config table; verify `tests/config/test_example.py` passes

## 2. Brains and the model client

- [x] 2.1 Add `agent/brains.py` with `Brains` (list with the startup tag added under its own name when unaliased, `current`, `tag`, `set`, `fall_back`, `is_default`, `names`) per design D1; unit tests in `tests/agent/test_brains.py` for the three "Configured brains" scenarios, set/unknown/already-current, and fall_back on and off the default
- [x] 2.2 Let `ModelClient.complete` take an optional `model` that overrides the constructor's (design D2); verify in `tests/agent/test_model.py` that the request body carries the override and the default when omitted

## 3. Telling the model and switching

- [x] 3.1 Add the brain line to `system_prompt` in `prompts.py` (design D4), for one and several brains, kept with a custom persona; verify in `tests/agent/test_prompts.py`
- [x] 3.2 In `agent/turn.py`, take `Brains` in `Agent`, send every request of a turn with the tag read at the turn's start, and pass the brain line to the prompt; log the brain in the turn's context line; verify in `tests/agent/test_turn.py` ("Another brain chosen", "After a switch")
- [x] 3.3 Add the owner-only `alfred_set_model` tool (design D3): enum of brain names, offered only with two or more brains, error result for an unknown name, "already in use" result, INFO log of old and new brain, switch applies from the next turn; verify each "Switching brains from the chat" scenario and "Switch logged" in `tests/agent/test_turn.py`

## 4. Fallback

- [x] 4.1 Add `notices.fell_back(address, failed, default)` in the butler register with the address once; verify in `tests/test_notices.py` ("Fallback notice")
- [x] 4.2 Implement the fallback in `Agent.run` (design D5): on `ModelFailed` on a non-default brain, fall back once, log a WARNING, run the turn again if no tool ran, else fail with the partial-completion notice; `TurnResult` and `TurnFailed` carry `fallback_notice`; verify every "Fallback to the startup brain" and "Turn runs again after a fallback" scenario plus "Fallback logged" in `tests/agent/test_turn.py`
- [x] 4.3 In the worker (`queue.py`), send `fallback_notice` as its own message before the reply or failure notice, for owner and `/ask` turns (including `NO_REPLY` and failed asks), without storing it in history; verify in `tests/test_queue.py`

## 5. Wiring

- [x] 5.1 Build `Brains` from `config.ai` in `app.serve`, pass it to `Agent`, keep summaries on the summary model, and list the brains and current brain in the startup log line; verify in `tests/test_serve.py` and, for "Ending a conversation on another brain", in `tests/memory/test_summarise.py` or `tests/test_queue.py`
- [x] 5.2 Run `make lint` and `make test` and verify both pass

## Workflow follow-up

- Bump the version and write the handover update (`homelab-handover` skill) noting the optional `[ai.models]` table.
- Archive the change once reviewed.
