# reminders Specification

## Purpose

Lets the owner ask alfred to remind them of something at a given time, and
has alfred bring it up in the Telegram chat when that time comes, including
after alfred was down.

## Requirements

### Requirement: Reminder tools in owner turns
In turns started by an owner message, alfred SHALL offer `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel`, handled by alfred itself. They SHALL NOT be offered in `/ask` turns or reminder turns.

#### Scenario: Owner turn
- **WHEN** the owner sends a message
- **THEN** the offered tools include `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel`

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** none of the reminder tools are offered

### Requirement: Setting a reminder
`alfred_remind_set` SHALL take the reminder text and exactly one of `in_minutes` (a positive whole number) or `at`. Due times SHALL be whole minutes; `in_minutes` SHALL drop the seconds, so the reminder is due at the minute the owner is told. It SHALL store the reminder durably and return the resolved day and 24-hour time in the owner's timezone, said as a person would, and the text, with no reminder number or countdown.

#### Scenario: Relative time
- **WHEN** at 16:47 Lisbon time on Monday 9 November 2026 the model calls `alfred_remind_set` with text "check the oven" and `in_minutes` 90
- **THEN** a reminder is stored for 18:17 that day, and the result gives "today at 18:17" and the text, and no number

#### Scenario: Confirmation
- **WHEN** the owner sends "remind me at five to call mum" at 14:47
- **THEN** alfred's reply confirms the reminder in its own words with the time 17:00, and mentions no reminder number

#### Scenario: Another day
- **WHEN** at 18:05 on Monday 9 November the model sets a reminder `at` "17:00"
- **THEN** the result gives "tomorrow at 17:00"

#### Scenario: Relative time with seconds
- **WHEN** at 13:55:37 the model calls `alfred_remind_set` with `in_minutes` 2
- **THEN** the reminder is due at 13:57:00, and the result gives "today at 13:57"

#### Scenario: Both or neither
- **WHEN** the model passes both `in_minutes` and `at`, or neither
- **THEN** the result is an error saying to pass exactly one, and nothing is stored

#### Scenario: Empty text
- **WHEN** the model passes an empty or blank text
- **THEN** the result is an error and nothing is stored

### Requirement: Clock time without a date
An `at` of the form `HH:MM` SHALL resolve to the next time that clock time occurs in the owner's timezone: today if it is still ahead, otherwise tomorrow.

#### Scenario: Still ahead today
- **WHEN** at 14:47 on Monday 9 November the model sets a reminder `at` "17:00"
- **THEN** it is due Monday 9 November at 17:00

#### Scenario: Already passed today
- **WHEN** at 18:05 on Monday 9 November the model sets a reminder `at` "17:00"
- **THEN** it is due Tuesday 10 November at 17:00

### Requirement: Date with or without a clock time
An `at` of the form `YYYY-MM-DD HH:MM` SHALL resolve to that local date and time. An `at` of the form `YYYY-MM-DD` SHALL use the configured default time on that date. A resolved time that is not in the future SHALL be an error, and nothing stored.

#### Scenario: Date and time
- **WHEN** the model sets a reminder `at` "2026-11-13 09:30"
- **THEN** it is due Friday 13 November 2026 at 09:30 Lisbon time

#### Scenario: Date only
- **WHEN** `[reminders] default_time` is not set and the model sets a reminder `at` "2026-11-13"
- **THEN** it is due Friday 13 November 2026 at 09:00

#### Scenario: In the past
- **WHEN** on Monday 9 November the model sets a reminder `at` "2026-11-08 10:00"
- **THEN** the result is an error saying the time has passed, and nothing is stored

#### Scenario: Today's default time passed
- **WHEN** at 11:00 on Monday 9 November the model sets a reminder `at` "2026-11-09" with the default time 09:00
- **THEN** the result is an error saying the time has passed, and nothing is stored

#### Scenario: Unreadable time
- **WHEN** the model sets a reminder `at` "five pm"
- **THEN** the result is an error naming the accepted forms, and nothing is stored

### Requirement: Listing reminders
`alfred_reminders_list` SHALL return the reminders that are set and not yet due, soonest first, each with its number, text, resolved time and how far away it is. With none set it SHALL say so.

#### Scenario: Two reminders
- **WHEN** reminders #12 (17:00 today) and #13 (18:17 today) are set
- **THEN** the list shows #12 first, then #13, each with its text, time and how far away it is

#### Scenario: Nothing set
- **WHEN** no reminders are set
- **THEN** the result says there are no reminders

### Requirement: Cancelling a reminder
`alfred_reminder_cancel` SHALL take a reminder number and cancel that reminder if it is set and not yet due, returning its text and time. Cancelling an unknown number, or a reminder already delivered or cancelled, SHALL be an error result.

#### Scenario: Cancel a set reminder
- **WHEN** the model cancels #12, which is set for 17:00
- **THEN** #12 is never delivered, and the result names "call mum" and 17:00

#### Scenario: Already delivered
- **WHEN** the model cancels #9, which was delivered yesterday
- **THEN** the result is an error saying #9 was already delivered

### Requirement: Due reminders are queued
alfred SHALL check for due reminders on the clock, at the start of every minute and at the configured interval within it, without making any AI request while none is due. Each due reminder SHALL be marked delivered and put into the message queue as a reminder item in one step, so it is queued exactly once even if alfred stops at that moment. Several due reminders SHALL be queued in due-time order.

#### Scenario: Reminder comes due
- **WHEN** reminder #12 is due at 17:00 and the check interval is 30 seconds
- **THEN** at 17:00:00 a reminder item for #12 is put in the queue, and #12 no longer appears in the list

#### Scenario: Nothing due
- **WHEN** no reminder is due for a whole day
- **THEN** alfred sends no request to the AI for reminders that day

### Requirement: Missed reminders are sent late
Reminders that came due while alfred was not running SHALL be queued when it starts, however late, and SHALL NOT be dropped.

#### Scenario: alfred down at the due time
- **WHEN** #12 was due at 17:00 and alfred was down from 16:00 to 19:40
- **THEN** after alfred starts, #12 is queued and reaches the owner

#### Scenario: Days late
- **WHEN** #12 was due three days before alfred starts
- **THEN** #12 is still queued and reaches the owner

### Requirement: Reminder turns
A reminder item SHALL run as an AI turn whose input is labelled as a reminder with its number, text, due time and the current time, so the model can say when it is late. The model SHALL be told not to mention the reminder's number. The model's answer SHALL be sent to the owner. `NO_REPLY` SHALL NOT be offered.

#### Scenario: On time
- **WHEN** #12 "call mum" is processed at 17:00
- **THEN** the model receives a reminder input naming #12, "call mum", due 17:00 and the current time 17:00, and the owner receives its answer

#### Scenario: Late
- **WHEN** #12 due at 17:00 is processed at 19:40
- **THEN** the model's input shows due 17:00 and now 19:40

### Requirement: A reminder always reaches the owner
If a reminder turn fails without a retry, or the model answers `NO_REPLY` or nothing, alfred SHALL send the owner the fixed reminder notice with the reminder's text and due time instead. A reminder turn that waits for an unreachable AI SHALL be retried like any other item.

#### Scenario: Model error
- **WHEN** the model answers a reminder turn with HTTP 400
- **THEN** the owner receives the fixed reminder notice naming "call mum" and 17:00

#### Scenario: Model says NO_REPLY
- **WHEN** the model answers a reminder turn with `NO_REPLY`
- **THEN** the owner receives the fixed reminder notice instead

#### Scenario: AI asleep
- **WHEN** #12 comes due while `ai.lan` is unreachable
- **THEN** the reminder item is retried with backoff and the owner receives the model's answer once the AI answers

### Requirement: Snoozing from the conversation
A delivered reminder SHALL be in the conversation with its full text, so the owner can answer it ("snooze 15 minutes", "remind me again at six") and the model can set a new reminder with the same text.

#### Scenario: Snooze
- **WHEN** reminder #12 "call mum" was delivered and the owner replies "snooze 15 minutes"
- **THEN** the model's context contains #12's text, and it can call `alfred_remind_set` with "call mum" and `in_minutes` 15
