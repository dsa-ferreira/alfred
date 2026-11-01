# Design

## Context

The system prompt is built in `src/alfred/agent/prompts.py`:
`system_prompt(configured, ask=..., unavailable=...)` returns either
`ai.system_prompt` or `DEFAULT_SYSTEM_PROMPT`, then appends the
unavailable-server note and, on `/ask` turns, `ASK_RULE`.
`DEFAULT_SYSTEM_PROMPT` mixes voice ("short and to the point", "no closing
offers") with operating rules (tool use, `[notice:x]`, `[ask:x]`). The
configured prompt replaces both.

Fixed notices are plain string constants spread over three modules:

| Where | Constant / literal | Reaches the owner |
| --- | --- | --- |
| `telegram/bot.py` | `TEXT_ONLY` | on any non-text owner message |
| `queue.py` | `GENERIC_FAILURE` | on an unexpected exception in a turn |
| `agent/turn.py` `_failed` | "Sorry, that request failed." | model error or empty reply |
| `agent/turn.py` `_ask_model` | "The AI is unreachable." | never: the failure is retryable, and the queue only sends notices for non-retryable failures |
| `agent/turn.py` `_ask_model` | "The AI became unreachable in the middle of that request." | after a tool ran |
| `agent/turn.py` `run` | "I stopped that request after too many steps." | step limit hit |
| `agent/turn.py` | `PARTIAL` suffix | appended when a tool ran |

Config sections are `extra="forbid"`, so unknown keys fail startup.

## Goals / Non-Goals

**Goals:**
- Voice and operating rules live in separate pieces of text. Only the voice
  can be replaced from config.
- One place defines every fixed notice, each built from the configured
  address.

**Non-Goals:**
- Changing `NEW_CONVERSATION_MARKER` ("— new conversation —"). It is a
  separator, not speech.
- Changing the `[service]` prefix that `/ask` replies get when sent.
- Making the operating rules, `ASK_RULE` or the `alfred_end_conversation`
  tool text configurable.
- Translating or localising anything.

## Decisions

### D1. Prompt order: persona, operating rules, turn additions

```
+------------------------------------------------+
| persona       ai.persona OR built-in butler    |  replaceable
+------------------------------------------------+
| operating     Telegram, tools, [notice:x],     |  built-in
| rules         [ask:x], follow-ups              |
+------------------------------------------------+
| turn adds     unavailable note, ASK_RULE       |  built-in, per turn
+------------------------------------------------+
```

`system_prompt(persona, *, owner_address, ask, unavailable)` joins the parts
with blank lines. The persona goes first so it frames who is speaking. The
rules and the turn additions follow, where a small model weighs them most.
The identity line ("You are alfred...") belongs to the persona. The
"chatting with your owner over Telegram" context belongs to the rules. The
rules refer to "the owner" in neutral terms and carry no voice.

*Alternative:* rules first, persona last. Rejected. The persona would then
sit between the rules and the per-turn additions, which splits the
mechanics into two places.

### D2. Built-in persona text, with example exchanges

The built-in persona is a template formatted with `owner_address`. It
states the register (formal, composed, courteous, never servile or chatty),
the address rule ("at most once per reply"), keeps today's brevity rules
verbatim in spirit, allows a rare dry remark, and covers `/ask`
interruptions ("open with a brief acknowledgement of the interruption, e.g.
'Pardon the interruption, {address}.'"). It ends with two or three short
example exchanges: an owner question, a tool-backed action and an ask
interruption. A 12B model follows a demonstrated register much more reliably
than a described one.

Draft:

```
You are alfred, the owner's butler, attending to them over Telegram.
Speak in a formal, composed English register: courteous, never servile,
never chatty. Address the owner as "{address}", at most once per reply.
Keep answers short, like a chat message: a sentence or a few short lines.
No preamble, no repeating the question, no closing offers of further
service. A dry remark is allowed only rarely, and never in place of the
answer. Use Markdown sparingly (bold, inline code, short lists) when it helps.
When you write to the owner on a service's behalf, you are interrupting
them: open with a brief acknowledgement, such as "Pardon the interruption,
{address}."

Examples of the register:
Owner: what's downloading?
alfred: Two items, {address}. Dune finishes within the hour; the other is stalled.
Owner: add a recipe for pancakes
alfred: Done, {address}. Saved as "Pancakes", 4 servings.
```

The final wording can be tuned during implementation. The spec scenarios
are the contract.

### D3. A custom persona is used verbatim

`ai.persona` is sent exactly as configured, without `str.format` or a
placeholder. Formatting user text would turn every literal `{` in it into a
crash risk, and the owner writing their own persona can write their own
address. Only the built-in template is formatted, and only the code
controls it.

### D4. `ai.owner_address`, default `"sir"`

The setting is `AIConfig.owner_address: str` with `min_length=1` and default
`"sir"`. It lives in `[ai]` beside `persona` because it shapes how alfred
speaks. Telegram only carries it. The value is used exactly as configured,
with no case changes. The notices always place it at the end of a clause
(", sir."), so a lowercase "sir" and a capitalised "Mr. Ferreira" both read
correctly.

### D5. Fixed notices in one module, built from the address

The notices move to a new `src/alfred/notices.py` that holds plain
functions. Each takes the address and returns the text:

```
text_only(a)            "I'm afraid I can only read text messages, {a}."
failed(a, partial)      "I'm afraid that request failed, {a}." [+ PARTIAL]
unreachable(a)          "The AI is unreachable at the moment, {a}."
unreachable_mid(a)      "The AI became unreachable partway through that request, {a}." + PARTIAL
too_many_steps(a, p)    "I stopped that request after too many steps, {a}." [+ PARTIAL]
PARTIAL                 " Some actions may have been partly carried out."
```

`PARTIAL` carries no address, so the address appears once per notice.
Wiring:

- `Agent` already holds `Config`, so it uses `cfg.ai.owner_address`.
- `Worker` (queue) gets a keyword argument `owner_address` for its generic
  failure, passed by `app.py`. Its `QueueConfig` stays focused on retries.
- `make_dispatcher(owner_user_id, queue, owner_address)`, passed by
  `app.py`.

`unreachable(a)` keeps a polished text even though the queue doesn't send
it today (see Context). Then nothing breaks if that path changes.

*Alternative:* keep the constants in place and `.format` them at each call
site. Rejected. Six strings stay scattered over three modules, and the next
voice change misses one.

### D6. Rename `system_prompt` to `persona`, no alias

`AIConfig.system_prompt` becomes `AIConfig.persona`. There is no deprecated
alias. The old key now fails validation with the existing "unknown key"
error, which names it. A silent alias would hide that the meaning changed,
from "the whole prompt" to "the voice only".

## Risks / Trade-offs

- [The 12B model overplays the persona: "sir" in every sentence, flourishes] →
  "at most once per reply" rule, terse examples, and the persona is
  replaceable from config without touching the rules.
- [The model ignores the brevity rule once it is in character] → The brevity
  rules stay in the persona with the same wording as today, and the examples
  are all one-liners.
- [A deployed config sets `system_prompt` and alfred refuses to start] → It's
  commented out in the example and the handover, so it is probably unset.
  The handover update calls out the rename. The error names the key.
- [Tests assert on exact notice strings] → Tests use the `notices` functions
  or check for the address, not copied literals.

## Migration Plan

1. Release as 0.2.0, because the config key rename is breaking.
2. Write a handover update (`homelab-handover` skill) that tells the homelab
   to rename `ai.system_prompt` to `ai.persona` if it is set, and mentions
   the optional `ai.owner_address`.
3. Rollback: redeploy 0.1.0. That only needs `persona` renamed back if it
   was set.
