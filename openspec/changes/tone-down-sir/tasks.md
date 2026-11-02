# Tasks

## 1. Persona text

- [x] 1.1 Rewrite the address rule in `BUTLER_PERSONA` (`src/alfred/agent/prompts.py`): use the address sparingly, only where a butler naturally would (an interruption, an apology, bad news, a formal acknowledgement), and in most replies not at all; formality comes from the register. Verify by reading the rendered `butler_persona("sir")`.
- [x] 1.2 Name a few formal phrasings that work without the address ("Certainly.", "Very good.", "I'm afraid...", "Shall I...?"), and keep the interruption opener "Pardon the interruption, {address}." unchanged. Verify `test_persona_covers_ask_interruptions` still passes.
- [x] 1.3 Rework the examples so the owner-turn replies carry no address (e.g. "Two items. Dune finishes within the hour; the other is stalled.", "Done. Saved as \"Pancakes\", 4 servings.") and the backup interruption example keeps it. Verify by reading the rendered persona.
- [x] 1.4 Update `tests/agent/test_prompts.py`: replace the "at most once per reply" assertion with one on the sparing rule, and add a test that the owner-turn examples contain no address while the interruption example does. Verify `make test` and `make lint` pass.
- [x] 1.5 Update the `ai.owner_address` row in `README.md` to say the address is used sparingly instead of "at most once per reply". Verify no "at most once per reply" remains (`grep -rn "once per reply" README.md src tests`).

## 2. Manual check

- [x] 2.1 Run alfred against the default model, start a fresh conversation and send several ordinary requests. Verify most replies have no "sir" and stay formal, and that an `/ask` interruption still opens with "Pardon the interruption, sir."

## Workflow follow-up

- Archive the change once the manual check is done.
