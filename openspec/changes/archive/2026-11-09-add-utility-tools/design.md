# Design

## Context

- The system prompt ends with today's date and no time of day (memory design
  D12, spec `agent-loop` "Current date in the system prompt"), so it changes
  at most once a day and Ollama can reuse the processed prefix. D12 rejected
  a date tool because small models often skip it.
- History entries already have `entries.created_at` (`db/models.py`), but
  `History.recent()` does not read it. `to_messages()` turns entries into chat
  messages, adding labels such as `[notice:<service>]`.
- An owner message is recorded when its turn starts (`queue._process`). The
  live input of an `/ask` turn is built in `agent/turn.py` and is recorded as
  history only after the turn, and only if the owner was told. The live input
  of a reminder turn already includes the time now.
- Reminders already keep the model away from date arithmetic: the model
  passes the time as the owner said it, and `reminders/when.py` resolves it
  against an injected `now` in `ai.timezone`.
- Tools follow the `LocalToolset` contract (`tools/base.py`) and are listed
  in order in `app.build_toolsets`.

## Goals / Non-Goals

**Goals:**
- Exact results for arithmetic, unit conversion and calendar questions, by
  either brain.
- The time of day available to the model without making cached prompt
  prefixes invalid.
- Nothing the model sends can run code or stall the process.

**Non-Goals:**
- Arithmetic on mixed units in one expression (`3 ft + 4 in`). The model
  converts each part and adds them in a second call.
- Volume-to-mass conversion for ingredients (would need a density table).
- Currency conversion (needs live rates; web search covers it).
- Natural-language dates in `alfred_date` ("next Friday"). The model turns
  them into `YYYY-MM-DD`, with the date from the prompt and `shift` to help.
- Times in archived transcripts, summaries or recalls (`memory/transcript.py`
  is unchanged).
- Random picks and subnet math (possible later changes).

## Decisions

### D1. The time of day comes from stamps on history messages, not the system prompt

`to_messages()` adds `[Mon 9 Nov 14:05] ` to the start of every `user`-role
entry: owner, notice, `ask_prompt`, reminder and recall. The time is the
entry's `created_at` in `ai.timezone`. Labels stay after the stamp
(`[Mon 9 Nov 14:20] [notice:mcparr] ...`). `agent/turn.py` stamps the live
`/ask` input with `now`. The live reminder input already says `now ...`, so it
is left as it is.

The stamp is absolute and has no year. A relative form ("today 14:05") would
change at midnight and rewrite every older message, which would make Ollama
reprocess the whole history. The year would only matter for a conversation
spanning New Year, and the weekday and date make it clear enough. Weekday and
month names come from `strftime` (`%a`, `%b`), as `format_when` already does.
alfred runs in the C locale, so they are in English.

Replies are not stamped, so the model doesn't learn to write stamps itself.

*Alternatives:* the time in the system prompt (rejected in D12: it would
change on every message); an `alfred_now` tool (the model often skips it, and
it costs a model request). `alfred_date now_in` with no zone still gives the
exact time when it matters, for example after a long tool loop.

*Cache:* old entries keep the same text, so the prefix stays valid until the
window starts dropping its oldest entries, which already happens today. The
recorded `ask_prompt` gets its `created_at` when the turn ends, so in later
turns its minute can differ from the stamp used during the turn. Only the
`/ask` turn's own cached prefix is affected.

### D2. Remove a stamp the model copies from the start of its reply

Small models imitate the format around them. Before the final answer is
checked for `NO_REPLY`, sent and recorded, `agent/turn.py` removes a leading
match of `^\[(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} (Jan|...|Dec) \d{2}:\d{2}\]\s*`.
Only that exact form at the very start is removed. The operating rules also
tell the model not to write stamps (D6).

### D3. `alfred_calculate`: a whitelisted AST evaluator, never `eval`

`ast.parse(expression, mode="eval")`, then a recursive walk that accepts only:
- `Constant` int/float (not bool, str or complex);
- `BinOp` with `Add Sub Mult Div FloorDiv Mod Pow`, and `UnaryOp` with
  `USub UAdd`;
- `Name` from `{pi, e}`;
- `Call` whose function is a bare `Name` from `{sqrt, abs, round, min, max,
  floor, ceil, log, log10, exp}`, with positional arguments only.

Anything else raises an error naming the node ("names other than pi and e
are not allowed: x").

Limits, each an error result rather than a stall:
- the expression is at most 500 characters;
- `**` is checked before it runs. Integer powers whose result would have more
  than 1000 digits (`|exp| * log10(|base|)`) are refused, and float overflow
  is caught. `9 ** 9 ** 9` is refused at the inner check before anything is
  computed;
- an integer result with more than 1000 digits is refused, which also limits
  long chains of multiplication;
- `ZeroDivisionError`, `ValueError` (math domain) and `OverflowError` become
  errors with plain wording.

Integers stay Python ints, so they are exact. A float result is formatted
with `format(x, ".12g")`, so `0.1 + 0.2` gives `0.3`, and an integral float
prints without `.0`. The result reads `347 * 28.5 = 9889.5`.

*Alternatives:* `simpleeval` (one more dependency for about 60 lines we can
test directly, and its defaults allow more than we need); `decimal`/`fractions`
throughout (exact, but `sqrt`/`log` and formatting get awkward; 12
significant digits is enough for a butler); a sandboxed Python (much more
to run and secure).

### D4. Units: `pint` is given only unit names, never the number

`alfred_calculate` takes the optional `from_unit` and `to_unit` and
converts with `ureg.Quantity(value, from_unit).to(to_unit)`. The value comes
from the D3 evaluator. Passing it to `Quantity` (instead of multiplying)
makes offset units such as `degF`/`degC` convert correctly.

Unit strings are still parsed by pint's own expression parser. The probe
showed that pint 0.26.2 does not run Python `eval`: `__import__` is just
an undefined unit. It does accept numeric exponents such as `m**99999999`. So
a unit string must be at most 40 characters, made only of letters, `°`,
`µ`, `_`, spaces, `/`, `*` and `^`, plus digits only as a single-digit
exponent straight after `^` or `**` (`m^2`, `m**3`). Anything else is
refused before pint sees it.

Errors map as follows: `DimensionalityError` → "cup (volume) can't be
converted to gram (mass) without more information, such as the ingredient's
density"; `UndefinedUnitError` and `DefinitionSyntaxError` → "unknown unit
'<x>'". The result names both quantities: `1.5 cup = 354.882 milliliter`.

The `UnitRegistry` is built once per process, on the first conversion (a
cached `registry()`, about 0.13 to 0.45 s measured), not per call and not at
startup, so a start with no conversions pays nothing. pint names to list in
the tool description: `cup`,
`tbsp`, `tsp`, `floz` (not `fl_oz`), `ml`, `l`, `g`, `kg`, `oz`, `lb`,
`degC`, `degF`, `km/h`, `mph`, `kcal`, `kJ`, `GB`, `MiB`. pint's `cup` is the
US cup (236.6 ml).

*Alternatives:* letting pint evaluate the whole expression (`"2.5*3 lb"`):
the number would go through pint's parser too, without D3's limits.
Separate `value` and unit parameters: the computed amount would need two
calls.

### D5. `alfred_date`: one tool with `op`, built on `reminders/when.py`

The parameters are `op` (enum `between | shift | weekday | now_in`), plus
`from`, `to`, `date`, `days`, `weeks`, `months` and `zone`. Each op checks
the parameters it needs and ignores the rest. One definition costs less
context than four, and the enum lists the operations in the schema.

- Dates go through a public `parse_date` added to `when.py` (today's
  private `_date` plus the `YYYY-MM-DD` regex and `today`), so both tools
  reject a bad date with the same `WhenError` wording. `today` uses the
  injected `now` in `ai.timezone`.
- `between` gives `(to - from).days`, also as weeks and days, and says
  "after" or "before" instead of a sign.
- `shift` adds `days + 7*weeks` with `timedelta`. It adds months by hand,
  limiting the day to the target month's length (`calendar.monthrange`).
  `OverflowError`/`ValueError` outside years 1 to 9999 becomes an error.
- `now_in` uses `ZoneInfo(zone)`. `ZoneInfoNotFoundError` or an invalid key
  becomes an error. The offset from the owner's zone is the difference of the
  two `utcoffset()` values at `now`, shown as "9 hours ahead" or "5 hours
  30 minutes behind". With no zone it gives the owner's local time.
- Dates are written like `format_when`: `Friday 25 December 2026`.

`dates.py` imports from `alfred.reminders.when`, as `reminders/tools.py`
already does. The helpers aren't moved into a new module yet, because this is
their only second user.

### D6. Operating rules

Two paragraphs are added to `OPERATING_RULES`:
- Times: "Messages from the owner, services and reminders start with the
  local time they arrived, such as [Mon 9 Nov 14:05]; the newest one is the
  time now. Never write these stamps in your replies."
- Calculation: "Never do arithmetic, percentages, unit conversions or date
  calculations in your head ... use alfred_calculate ... alfred_date ...
  and answer from their results." Percentages are written as `/100` (`%` is
  modulo), and the tool description says so as well.

Labels now come after the stamp, so the existing "A message starting with
[notice:<service>]" lines become "A message labelled [notice:<service>] ...".
The prompt tests that check for these phrases change with them.

### D7. Wiring

`tools/calculator.py` (`CalculatorTools`) and `tools/dates.py`
(`DateTools`) are `LocalToolset`s with the default `offered` (every turn),
because they belong to no feature. `DateTools(zone, now=...)` takes the
clock as `ReminderTools` does. `build_toolsets` adds both after
`BrainTools` and before `McpTools`. No configuration is added: the limits in
D3 and D4 are for safety, not for the owner to tune.

## Risks / Trade-offs

- [The model still does math in its head, or skips the tool] → the rule in
  D6, plus tool descriptions that start with "Use for any arithmetic...".
  Check with a few manual turns on both brains after deploying (task 6).
- [The model copies stamps somewhere other than the start of a reply] → only
  the start is cleaned. Anything else is cosmetic and the rule discourages it.
- [About 20 more characters per history message] → about 600 characters for a
  30-entry window. This shows up in the context-size log line.
- [US cup vs metric cup (250 ml)] → the result names the unit pint used. A
  custom metric-cup definition can be added later if it matters.
- [pint's unit parser is not ours] → only checked unit strings reach it
  (D4), and its exceptions become error results through `safe_call` anyway.
- [Stamp minute of an `/ask` prompt differs between its turn and later turns]
  → this is cosmetic, see D1.

## Migration Plan

No schema or configuration change. Deploy is an image rebuild for `pint`, and
rollback is the previous image. Stamps appear on all history at once,
including entries recorded before the upgrade, because `created_at` has
always been set.
