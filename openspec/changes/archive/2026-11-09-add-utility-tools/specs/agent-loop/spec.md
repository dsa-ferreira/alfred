# Spec Delta

## ADDED Requirements

### Requirement: Calculation routing rules
The operating rules SHALL tell the model never to do arithmetic, unit conversions or date calculations itself (sums, percentages, scaling, days between dates, the weekday of a date, adding days or months, the time in another timezone), but to use `alfred_calculate` and `alfred_date` and answer from their results.

#### Scenario: Rules with a custom persona
- **WHEN** any turn runs, with or without a custom persona
- **THEN** the operating rules name `alfred_calculate` for arithmetic and unit conversions and `alfred_date` for date calculations and other timezones, and say never to work these out itself

### Requirement: Message times explained
The operating rules SHALL tell the model that messages from the owner, services and reminders start with the local time they arrived, that the newest one gives the current time of day, and that it must never write such a time stamp in its own replies.

#### Scenario: Time of day question
- **WHEN** any turn runs
- **THEN** the operating rules explain the `[Mon 9 Nov 14:05]` stamps, say the newest gives the time now, and say not to write them in replies

### Requirement: Echoed time stamp removed from replies
When the model's final answer starts with a message time stamp in the history's form, such as `[Mon 9 Nov 14:05]`, alfred SHALL remove that stamp, and the whitespace after it, before sending and recording the answer. Brackets anywhere else in the answer SHALL be left as they are.

#### Scenario: Echoed stamp
- **WHEN** the model answers `[Mon 9 Nov 14:06] It is just past two, sir.`
- **THEN** the owner receives `It is just past two, sir.`, and that is what the history records

#### Scenario: Other brackets kept
- **WHEN** the model answers `Saved [draft] for later.`
- **THEN** the answer is sent unchanged
