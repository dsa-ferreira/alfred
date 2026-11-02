# Proposal

## Why

The butler calls the owner "sir" in nearly every reply, and it has grown
tiresome. The persona says "at most once per reply", but every example reply
uses the address, and the model copies the examples rather than the limit.
The butler should come across as formal through its register, using the
address only now and then, where a butler naturally would.

## What Changes

- The built-in persona tells the model to use the form of address sparingly:
  only where a butler naturally would (an interruption, an apology, bad news,
  a formal acknowledgement), and in most replies not at all.
- The persona's example replies mostly go without the address. The
  interruption example keeps it.
- The persona names formal phrasings that carry the register without the
  address ("Certainly.", "Very good.", "I'm afraid...", "Shall I...?").
- Unchanged: the interruption opener ("Pardon the interruption, {address}."),
  the configurable form of address and its default "sir", the fixed notices
  (they still end with the address), and custom personas.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `persona`: the *Form of address* requirement changes from "at most once
  per reply" to "sparingly, only where a butler naturally would; most replies
  use none".

## Impact

- `src/alfred/agent/prompts.py`: `BUTLER_PERSONA` text.
- `tests/agent/test_prompts.py`: the assertion on the address wording.
- `README.md`: the `ai.owner_address` row says "at most once per reply".
- No config, API or schema changes. The model's actual restraint can't be
  unit-tested; it is checked by hand in a fresh conversation, since old
  replies in an existing conversation keep showing the model the old habit.
