# Spec Delta

## MODIFIED Requirements

### Requirement: Turn context
Each turn SHALL send the system prompt, the current conversation's recent history, and the new input. For an `/ask` turn, the input SHALL be labelled with the service name and the model told about the `NO_REPLY` convention. For a reminder turn, the input SHALL be labelled as a reminder with its number, due time and the current time, and the model told to write the reminder for the owner.

#### Scenario: Ask turn
- **WHEN** service `disk-check` asks "is this worth telling the owner?"
- **THEN** the model receives the recent history, an input identifying `disk-check` as the sender, and the `NO_REPLY` instruction

#### Scenario: Reminder turn
- **WHEN** reminder #12 "call mum" due at 17:00 is processed at 17:00
- **THEN** the model receives the recent history, an input labelled as reminder #12 with its due time and the current time, an instruction to write the reminder for the owner, and no `NO_REPLY` instruction

## ADDED Requirements

### Requirement: Reminder routing rules
The operating rules SHALL tell the model: a request that says "task" or "todo" goes to the owner's to-do list tools, even when it has a time; any other request with a time or date is a reminder set with `alfred_remind_set`; any other request to be reminded goes to the to-do list tools. They SHALL tell the model to pass times as the owner said them and to confirm in its own words.

#### Scenario: Timed request
- **WHEN** any turn runs, with or without a custom persona
- **THEN** the operating rules say that "remind me to call my mother at five" is a reminder for `alfred_remind_set`, and that times are passed as `in_minutes` or `at` and confirmed in its own words from the tool's result

#### Scenario: Task keyword wins
- **WHEN** any turn runs
- **THEN** the operating rules say that "add a task to call mum at five" goes to the to-do list tools, not to `alfred_remind_set`

#### Scenario: No time
- **WHEN** any turn runs
- **THEN** the operating rules say that "remind me to buy groceries" goes to the to-do list tools

#### Scenario: No reminder numbers
- **WHEN** any turn runs
- **THEN** the operating rules tell the model never to mention reminder numbers to the owner except when listing reminders

### Requirement: Reminder messages explained
The operating rules SHALL explain that a message starting with `[reminder:#<id> …]` is a reminder the owner set earlier that alfred delivered, not something the owner just said, and that the assistant message after it is what the owner was told.

#### Scenario: Reminder in the history
- **WHEN** a custom persona is configured and the current conversation contains a delivered reminder
- **THEN** the system prompt still explains `[reminder:#<id> …]` messages
