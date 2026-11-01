# Proposal

## Why

alfred replies in a neutral assistant voice that doesn't fit its name. The
owner wants a butler: formal and courteous, addressing them as "sir", while
keeping replies as short as they are today. Today the only way to change the
voice is `ai.system_prompt`, which replaces the whole built-in prompt. That
includes the tool rules and the `[notice:x]`/`[ask:x]` conventions, so
tuning the voice can quietly break how alfred handles service messages.

## What Changes

- Split the system prompt into two layers: a **persona** (voice only,
  replaceable from config) and built-in **mechanics** (tool use, notice and
  ask conventions, the `NO_REPLY` rule, unavailable-server notes). The
  mechanics can no longer be replaced from config.
- Make the built-in persona a formal, terse English butler: it addresses
  the owner with a configured form of address at most once per reply, makes
  a dry remark only rarely, and keeps today's brevity rules (no preamble, no
  repeating the question, no closing offers of further service). It also
  says how to open a message that interrupts the owner on a service's
  behalf.
- Add an `ai.owner_address` setting (default `"sir"`). The built-in persona
  and the fixed notices use it.
- Rewrite the fixed notices that skip the model in the same butler register,
  ending with the configured address: the text-only notice, the generic
  failure, AI unreachable, AI unreachable mid-request, too many steps, and
  the "some actions may have partly completed" suffix. They are not
  configurable.
- **BREAKING**: rename `ai.system_prompt` to `ai.persona`. Because unknown
  keys are rejected, a config that still sets `system_prompt` fails at
  startup with an error naming the key.

## Capabilities

### New Capabilities
- `persona`: alfred's voice toward the owner: the built-in butler persona,
  the configurable form of address, and the fixed notices sent without the
  model speaking in that voice.

### Modified Capabilities
- `agent-loop`: the "Telegram-oriented system prompt" requirement changes
  from one replaceable prompt to a configurable persona layered over
  built-in mechanics that cannot be replaced.
- `service-configuration`: the list of tunables replaces "system prompt"
  with the persona and the owner's form of address.

## Impact

- Code: `src/alfred/agent/prompts.py` (split into persona and mechanics),
  `src/alfred/agent/turn.py` (failure notices, wiring the persona),
  `src/alfred/config.py` (`AIConfig.persona`, `AIConfig.owner_address`),
  `src/alfred/queue.py` (generic failure notice), `src/alfred/telegram/bot.py`
  (text-only notice), `src/alfred/app.py` (passing the address).
- Tests: `tests/agent/test_prompts.py`, `tests/agent/test_turn.py`,
  `tests/test_queue.py`, `tests/telegram/test_bot.py`, `tests/config/`.
- Config and docs: `alfred.toml.example`, and a handover update for the
  homelab noting the `system_prompt` -> `persona` rename and the new
  `owner_address` key.
- No new dependencies, no database or API changes.
