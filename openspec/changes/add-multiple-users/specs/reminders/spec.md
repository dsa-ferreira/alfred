# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: Reminder tools in owner turns`
- TO: `### Requirement: Reminder tools in user turns`

- FROM: `### Requirement: A reminder always reaches the owner`
- TO: `### Requirement: A reminder always reaches its recipients`

## MODIFIED Requirements

### Requirement: Reminder tools in user turns
In turns started by a user's message, alfred SHALL offer `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel` when the user's allow-list matches them, handled by alfred itself. They SHALL NOT be offered in `/ask` turns or reminder turns.

#### Scenario: Owner turn
- **WHEN** a user whose allow-list includes `alfred_*` sends a message
- **THEN** the offered tools include `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel`

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** none of the reminder tools are offered

### Requirement: Setting a reminder
`alfred_remind_set` SHALL take the reminder text, exactly one of `in_minutes` (a positive whole number) or `at`, and an optional `for`: a list of configured user names to remind, defaulting to the turn's user alone. Due times SHALL be whole minutes; `in_minutes` SHALL drop the seconds, so the reminder is due at the minute the user is told. It SHALL store the reminder durably with the turn's user as its creator and the named users as its recipients, and return the resolved day and 24-hour time in the configured timezone, said as a person would, the text and, when the recipients are not the turn's user alone, their names, with no reminder number or countdown.

#### Scenario: Relative time
- **WHEN** at 16:47 Lisbon time on Monday 9 November 2026 the model calls `alfred_remind_set` with text "check the oven" and `in_minutes` 90
- **THEN** a reminder for the turn's user is stored for 18:17 that day, and the result gives "today at 18:17" and the text, and no number

#### Scenario: Confirmation
- **WHEN** the user sends "remind me at five to call mum" at 14:47
- **THEN** alfred's reply confirms the reminder in its own words with the time 17:00, and mentions no reminder number

#### Scenario: Another day
- **WHEN** at 18:05 on Monday 9 November the model sets a reminder `at` "17:00"
- **THEN** the result gives "tomorrow at 17:00"

#### Scenario: Relative time with seconds
- **WHEN** at 13:55:37 the model calls `alfred_remind_set` with `in_minutes` 2
- **THEN** the reminder is due at 13:57:00, and the result gives "today at 13:57"

#### Scenario: Remind another user
- **WHEN** on `diogo`'s turn the model sets "buy milk" `at` "18:00" `for` `["ana"]`
- **THEN** the reminder is stored with creator `diogo` and recipient `ana` only, and the result names `ana`

#### Scenario: Remind both
- **WHEN** on `ana`'s turn the model sets "dinner with my parents" `for` `["ana", "diogo"]`
- **THEN** the reminder has both as recipients

#### Scenario: Unknown user
- **WHEN** the model passes `for` `["bob"]` and no user is named `bob`
- **THEN** the result is an error listing the users that can be reminded, and nothing is stored

#### Scenario: Both or neither
- **WHEN** the model passes both `in_minutes` and `at`, or neither
- **THEN** the result is an error saying to pass exactly one, and nothing is stored

#### Scenario: Empty text
- **WHEN** the model passes an empty or blank text
- **THEN** the result is an error and nothing is stored

### Requirement: Listing reminders
`alfred_reminders_list` SHALL return the reminders that are set and not yet due on which the turn's user is the creator or a recipient, soonest first, each with its number, text, resolved time, how far away it is, and, when it is not the turn's user's alone, its creator and recipients. With none set it SHALL say so.

#### Scenario: Two reminders
- **WHEN** reminders #12 (17:00 today) and #13 (18:17 today) are set for the turn's user
- **THEN** the list shows #12 first, then #13, each with its text, time and how far away it is

#### Scenario: Shared and someone else's
- **WHEN** `diogo` set #14 for `ana`, and #15 for both of them
- **THEN** `ana`'s list shows #14 as set by `diogo`, and #15 with both recipients, and `diogo`'s list shows both too

#### Scenario: Not involved
- **WHEN** `ana` set #16 for herself only
- **THEN** `diogo`'s list does not show #16

#### Scenario: Nothing set
- **WHEN** no reminders are set that involve the turn's user
- **THEN** the result says there are no reminders

### Requirement: Cancelling a reminder
`alfred_reminder_cancel` SHALL take a reminder number and cancel that reminder for all its recipients if it is set and not yet due and the turn's user is its creator or a recipient, returning its text and time. Cancelling an unknown number, a reminder the turn's user is not involved in, or a reminder already delivered or cancelled, SHALL be an error result. A reminder the user is not involved in SHALL be refused exactly as an unknown number is.

#### Scenario: Cancel a set reminder
- **WHEN** the model cancels #12, which is set for 17:00
- **THEN** #12 is never delivered, and the result names "call mum" and 17:00

#### Scenario: A recipient cancels a shared reminder
- **WHEN** `ana` cancels #15, which `diogo` set for both of them
- **THEN** #15 is delivered to neither

#### Scenario: Not involved
- **WHEN** `diogo` cancels #16, which `ana` set for herself
- **THEN** the result is the same error as for an unknown number, and #16 stays set

#### Scenario: Already delivered
- **WHEN** the model cancels #9, which was delivered yesterday
- **THEN** the result is an error saying #9 was already delivered

### Requirement: Due reminders are queued
alfred SHALL check for due reminders on the clock, at the start of every minute and at the configured interval within it, without making any AI request while none is due. Each due reminder SHALL be marked delivered and put into the message queue as one reminder item per recipient in one step, so each recipient's item is queued exactly once even if alfred stops at that moment. Several due reminders SHALL be queued in due-time order.

#### Scenario: Reminder comes due
- **WHEN** reminder #12 is due at 17:00 and the check interval is 30 seconds
- **THEN** at 17:00:00 a reminder item for #12 is put in the queue, and #12 no longer appears in the list

#### Scenario: Shared reminder comes due
- **WHEN** #15, for `diogo` and `ana`, comes due
- **THEN** two reminder items are queued, one for each

#### Scenario: Nothing due
- **WHEN** no reminder is due for a whole day
- **THEN** alfred sends no request to the AI for reminders that day

### Requirement: Reminder turns
A reminder item SHALL run as an AI turn for its recipient, in their conversation, whose input is labelled as a reminder with its number, text, due time, the current time and, when the recipient did not set it, who did, so the model can say when it is late and who asked. The model SHALL be told not to mention the reminder's number. The model's answer SHALL be sent to that recipient. `NO_REPLY` SHALL NOT be offered.

#### Scenario: On time
- **WHEN** #12 "call mum" is processed at 17:00
- **THEN** the model receives a reminder input naming #12, "call mum", due 17:00 and the current time 17:00, and the recipient receives its answer

#### Scenario: Late
- **WHEN** #12 due at 17:00 is processed at 19:40
- **THEN** the model's input shows due 17:00 and now 19:40

#### Scenario: Set by someone else
- **WHEN** `diogo` set #14 "buy milk" for `ana`
- **THEN** `ana`'s reminder turn input says `diogo` set it, and her answer is sent only to her

### Requirement: A reminder always reaches its recipients
If a recipient's reminder turn fails without a retry, or the model answers `NO_REPLY` or nothing, alfred SHALL send that recipient the fixed reminder notice with the reminder's text and due time instead. A reminder turn that waits for an unreachable AI SHALL be retried like any other item.

#### Scenario: Model error
- **WHEN** the model answers a reminder turn with HTTP 400
- **THEN** the recipient receives the fixed reminder notice naming "call mum" and 17:00

#### Scenario: Model says NO_REPLY
- **WHEN** the model answers a reminder turn with `NO_REPLY`
- **THEN** the recipient receives the fixed reminder notice instead

#### Scenario: AI asleep
- **WHEN** #12 comes due while `ai.lan` is unreachable
- **THEN** the reminder item is retried with backoff and the recipient receives the model's answer once the AI answers

## ADDED Requirements

### Requirement: Reminders reach users without reminder tools
A user SHALL receive reminders others set for them even when their allow-list does not match the reminder tools.

#### Scenario: Recipient without the tools
- **WHEN** `ana`'s allow-list has no reminder tools and `diogo` sets #14 for her
- **THEN** #14 is delivered to `ana`
