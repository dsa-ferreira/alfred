# Tasks

## 1. Configuration

- [x] 1.1 In `src/alfred/config.py`, rename `AIConfig.system_prompt` to `persona` and add `owner_address: str` (`min_length=1`, default `"sir"`) (design D4, D6); update `tests/config/test_config.py` so defaults (`persona is None`, `owner_address == "sir"`) and custom values load, and add a test that `[ai] system_prompt = "..."` fails with an error naming the unknown key; verify with `make test`
- [x] 1.2 Update `alfred.toml.example`: replace the `system_prompt` comment with a commented `persona` (says it replaces only the voice, not the operating rules) and an `owner_address = "sir"` line; adjust `tests/config/test_example.py` for the renamed key; verify the example tests pass

## 2. Layered system prompt

- [x] 2.1 In `src/alfred/agent/prompts.py`, split `DEFAULT_SYSTEM_PROMPT` into a built-in persona template (formatted with the address, with the butler register, brevity rules, rare dry remark, `/ask` interruption opening and two or three example exchanges, per design D2) and neutral operating rules (Telegram context, tools, `[notice:x]`, `[ask:x]`, follow-ups); change `system_prompt` to `system_prompt(persona, *, owner_address, ask, unavailable)` joining persona, rules and turn additions in that order (D1); a custom persona is used verbatim (D3)
- [x] 2.2 Rewrite `tests/agent/test_prompts.py`: default prompt contains the persona with "sir" and the operating rules; a custom address appears in the built-in persona; a custom persona replaces the butler text but `[notice:`, `[ask:` and the Telegram context are still present; a custom persona containing `{` is sent unchanged; `NO_REPLY` and the unavailable note still appear; verify with `make test`
- [x] 2.3 In `src/alfred/agent/turn.py`, pass `cfg.ai.persona` and `cfg.ai.owner_address` to `system_prompt`; verify `tests/agent/test_turn.py` passes

## 3. Fixed notices

- [x] 3.1 Add `src/alfred/notices.py` with `text_only`, `failed`, `unreachable`, `unreachable_mid`, `too_many_steps` and the `PARTIAL` suffix (design D5), each ending its first clause with the address used as written; add `tests/test_notices.py` checking each notice ends with ", {address}." (before any partial suffix), the address appears exactly once, and "Mr. Ferreira" is used unchanged; verify with `make test`
- [x] 3.2 Use the notices in `src/alfred/agent/turn.py` (`_failed`, `_ask_model`, the step-limit failure) with `cfg.ai.owner_address`, removing the old literals and the local `PARTIAL`; update `tests/agent/test_turn.py` assertions to the new notices; verify with `make test`
- [x] 3.3 Give `Worker` an `owner_address` keyword argument and use `notices.failed` for the unexpected-error notice, removing `GENERIC_FAILURE`; update `Worker(...)` construction in `tests/test_queue.py` and `tests/test_api.py` and the failure assertions; verify with `make test`
- [x] 3.4 Change `make_dispatcher(owner_user_id, queue, owner_address)` to send `notices.text_only(owner_address)`, removing `TEXT_ONLY`; update `tests/telegram/test_bot.py`; verify with `make test`
- [x] 3.5 Wire `config.ai.owner_address` into `Worker` and `make_dispatcher` in `src/alfred/app.py`; verify `tests/test_serve.py` passes and `grep -rn "Sorry" src/alfred` finds nothing

## 4. Integration and release

- [x] 4.1 Run `make lint` and `make test`; both pass
- [x] 4.2 Manual check against the real model: run `make run`, send a question, an action and a non-text message, and trigger an `/ask`; confirm replies are formal, short, use "sir" at most once, and that the `/ask` reply opens with an acknowledgement of the interruption; tune the built-in persona wording if needed
- [x] 4.3 Bump the version to 0.2.0 in `pyproject.toml` (breaking config rename); verify `make image` tags `alfred:0.2.0`
- [x] 4.4 Write the handover update with the `homelab-handover` skill: rename `ai.system_prompt` to `ai.persona` if set, optional `ai.owner_address`; verify the update file exists under `handover/`

## Workflow follow-up

- Archive the change with `/opsx:archive` once implemented and deployed.
