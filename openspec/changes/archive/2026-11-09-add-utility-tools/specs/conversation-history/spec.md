# Spec Delta

## ADDED Requirements

### Requirement: Messages carry the time they arrived
Every history entry the model receives as a `user` message (owner messages, notices, `/ask` prompts, delivered reminders and recalls) SHALL start with the local time it was recorded, in the configured timezone, as `[<weekday> <day> <month> <HH:MM>]`, such as `[Mon 9 Nov 14:05]`. The live input of an `/ask` turn SHALL carry the current time the same way. Replies SHALL NOT be stamped.

#### Scenario: Owner message
- **WHEN** the owner wrote "what time is it?" at 14:05 Lisbon time on Monday 9 November 2026, with the timezone set to `Europe/Lisbon`
- **THEN** the model receives it as `[Mon 9 Nov 14:05] what time is it?`

#### Scenario: Notice
- **WHEN** `mcparr` notified "Dune finished" at 14:20 on Monday 9 November 2026
- **THEN** the model receives `[Mon 9 Nov 14:20] [notice:mcparr] Dune finished`

#### Scenario: Reply not stamped
- **WHEN** the history contains alfred's reply "It is just past two."
- **THEN** the model receives that reply as written, without a time

#### Scenario: Live ask input
- **WHEN** service `disk-check` asks at 03:00 on Tuesday 10 November 2026
- **THEN** the turn's input starts with `[Tue 10 Nov 03:00] [ask:disk-check]`

### Requirement: Message times never change
The time on a history entry SHALL depend only on when it was recorded and the configured timezone, never on the current time, so the same entry is sent with the same text on every later turn.

#### Scenario: Next day
- **WHEN** an owner message recorded at 14:05 on Monday 9 November 2026 is still in the window on Tuesday 10 November
- **THEN** it is still sent as `[Mon 9 Nov 14:05] ...`, with no relative wording such as "yesterday"
