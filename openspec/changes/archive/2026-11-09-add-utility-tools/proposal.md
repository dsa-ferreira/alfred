# Proposal

## Why

Both brains alfred runs on (gemma4 26B-A4B QAT and bonsai 27B) answer
arithmetic, unit conversions and calendar questions from memory, and they get
them wrong: a wrong product, a wrong weekday for a date, a wrong count of days.
alfred also knows today's date but not the time of day, which it left out of
the system prompt on purpose to keep the prompt cache stable (memory design
D12). The fix is to have alfred compute these answers instead of the model, as
reminders already do for due times.

## What Changes

- **Message times in the history.** Every message from the owner, a service or
  a reminder (the `user` role) starts with the absolute local time it arrived,
  such as `[Mon 9 Nov 14:05]`. The model gets the time of day and how much time
  passed between messages. Old messages never change, so the history prefix
  stays cacheable. The system prompt keeps only the date. A stamp the model
  copies to the start of its reply is removed.
- **`alfred_calculate`**: evaluates an arithmetic expression exactly and
  safely (never with `eval`), and converts the result between units when
  given `from_unit` and `to_unit` (cooking volumes and weights, temperatures,
  lengths, data sizes and so on).
- **`alfred_date`**: date arithmetic in the owner's timezone. It gives the
  days between two dates, shifts a date by days, weeks or months, gives the
  weekday of a date, and gives the current date and time in any IANA
  timezone.
- **Operating rules**: they explain the message times and tell the model
  never to do arithmetic, unit conversions or date calculations itself, but
  to use these tools and report their results.
- Both tools are offered on every turn (owner, `/ask` and reminder).

## Capabilities

### New Capabilities

- `calculator`: the `alfred_calculate` tool, which does exact arithmetic
  and unit conversion for the model.
- `date-tools`: the `alfred_date` tool, which does calendar arithmetic and
  timezone lookups in the owner's timezone.

### Modified Capabilities

- `conversation-history`: the history the model receives stamps each message
  from the owner, a service or a reminder with the absolute local time it
  arrived.
- `agent-loop`: the operating rules explain the message times and route all
  arithmetic, unit conversion and date calculation to the tools, and a stamp
  echoed at the start of a reply is removed.

## Impact

- Code: `history.py` (`recent()` also reads `created_at`; `to_messages()`
  stamps entries), `agent/turn.py` (stamps the live `/ask` input, removes an
  echoed stamp from replies), `agent/prompts.py` (operating rules), new
  `tools/calculator.py` and `tools/dates.py`, `app.py` (`build_toolsets`),
  README.
- Dependency: `pint` (unit registry). There is no database migration, because
  `entries.created_at` already exists, and no new configuration.
- Each turn's context grows by the two tool definitions plus about 20
  characters per history message.
- Deployment: only an image rebuild for the new dependency. Nothing changes in
  the homelab.
