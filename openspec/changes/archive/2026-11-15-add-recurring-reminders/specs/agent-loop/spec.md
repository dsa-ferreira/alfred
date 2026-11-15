# Spec Delta

## MODIFIED Requirements

### Requirement: Turn context
Each turn SHALL send the system prompt, the recent history of the turn's user's current conversation, and the new input. For an `/ask` turn, the input SHALL be labelled with the service name and the model told about the `NO_REPLY` convention. For a reminder turn, the input SHALL be labelled as a reminder with its number, due time and the current time, and the model told to carry out with its tools what the reminder asks it to find out or check, then write the message for its recipient; for a quiet reminder, also that it may answer `NO_REPLY`.

#### Scenario: Ask turn
- **WHEN** service `disk-check` asks the admin "is this worth telling the owner?"
- **THEN** the model receives the admin's recent history, an input identifying `disk-check` as the sender, and the `NO_REPLY` instruction

#### Scenario: Reminder turn
- **WHEN** reminder #12 "call mum" due at 17:00 is processed at 17:00
- **THEN** the model receives the recipient's recent history, an input labelled as reminder #12 with its due time and the current time, an instruction to write the reminder for the recipient, and no `NO_REPLY` instruction

#### Scenario: Reminder turn that asks for something
- **WHEN** reminder #31 "brief me on the weather" is processed
- **THEN** the instruction tells the model to look up what the reminder asks for with its tools before writing the message, and does not tell it to avoid tools

#### Scenario: Quiet reminder turn
- **WHEN** quiet reminder #33 "tell me only if something is downloading" is processed
- **THEN** the instruction tells the model to answer exactly `NO_REPLY` when there is nothing to tell

### Requirement: Reminder routing rules
When `alfred_remind_set` is offered on the turn, the operating rules SHALL tell the model: a request that says "task" or "todo" goes to the user's to-do list tools, even when it has a time; any other request with a time or date, or to do something later or on a schedule, is a reminder set with `alfred_remind_set` and is not done now; any other request to be reminded goes to the to-do list tools. They SHALL tell the model to pass times as the user said them, to pass `repeat` for "every …" requests and `quiet_ok` for "only if …" ones, to confirm in its own words, and to pass `for` with user names when the user asks to remind someone else or several people.

#### Scenario: Timed request
- **WHEN** a turn offering `alfred_remind_set` runs, with or without a custom persona
- **THEN** the operating rules say that "remind me to call my mother at five" is a reminder for `alfred_remind_set`, and that times are passed as `in_minutes` or `at` and confirmed in its own words from the tool's result

#### Scenario: Task keyword wins
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules say that "add a task to call mum at five" goes to the to-do list tools, not to `alfred_remind_set`

#### Scenario: No time
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules say that "remind me to buy groceries" goes to the to-do list tools

#### Scenario: No reminder numbers
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules tell the model never to mention reminder numbers to the user except when listing reminders

#### Scenario: Remind someone else
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules say that "remind ana to buy milk at six" sets a reminder `for` `ana`

#### Scenario: Reminder tools not offered
- **WHEN** the turn's user's allow-list does not match `alfred_remind_set`
- **THEN** the operating rules contain no reminder routing rules

#### Scenario: Scheduled briefing
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules say that "every weekday at 8, brief me on the weather" is a reminder with `repeat` `weekdays` and `at` "08:00", and that the weather is not looked up now

#### Scenario: Only if
- **WHEN** a turn offering `alfred_remind_set` runs
- **THEN** the operating rules say that "every evening at 9, tell me if anything is downloading" is a `daily` reminder with `quiet_ok`
