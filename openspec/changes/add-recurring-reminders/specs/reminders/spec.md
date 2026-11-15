# Spec Delta

## ADDED Requirements

### Requirement: Repeating reminders
`alfred_remind_set` SHALL take an optional `repeat`: `daily`, `weekdays` (Monday to Friday), `weekends` (Saturday and Sunday) or `weekly`, the last with `days`, a list of weekday names. A repeating reminder SHALL take `at` as `HH:MM` and SHALL be due at that local time on every day its rule matches, under one reminder number, until cancelled.

#### Scenario: Every weekday
- **WHEN** at 14:47 on Sunday 15 November 2026 the model sets "brief me on the weather" with `repeat` `weekdays` and `at` "08:00"
- **THEN** one reminder is stored, first due Monday 16 November at 08:00, and then each weekday at 08:00

#### Scenario: Weekly on given days
- **WHEN** at 21:00 on Tuesday 17 November the model sets "take the bins out" with `repeat` `weekly`, `days` `["tue", "fri"]` and `at` "20:00"
- **THEN** it is first due Friday 20 November at 20:00, then Tuesday 24 November at 20:00

#### Scenario: Today still ahead
- **WHEN** at 07:30 on Monday 16 November the model sets a `daily` reminder `at` "08:00"
- **THEN** it is first due that day at 08:00

#### Scenario: Result
- **WHEN** a `weekdays` reminder at 08:00 is set on Sunday 15 November
- **THEN** the result gives the rule "every weekday at 08:00" and the first time "tomorrow at 08:00", and no number

### Requirement: Repeat rule errors
A `repeat` other than the four rules, `days` without `repeat` `weekly`, `weekly` without `days` or with an unknown day name, a repeating reminder with `in_minutes`, or one with an `at` that is not `HH:MM` SHALL be an error result naming the accepted forms, and nothing SHALL be stored.

#### Scenario: Monthly
- **WHEN** the model passes `repeat` `monthly`
- **THEN** the result is an error listing `daily`, `weekdays`, `weekends` and `weekly`, and nothing is stored

#### Scenario: Weekly without days
- **WHEN** the model passes `repeat` `weekly` and no `days`
- **THEN** the result is an error saying `weekly` needs `days`, and nothing is stored

#### Scenario: Repeat with a date
- **WHEN** the model passes `repeat` `daily` and `at` "2026-11-20 08:00"
- **THEN** the result is an error saying a repeating reminder takes `at` as `HH:MM`, and nothing is stored

#### Scenario: Repeat with minutes
- **WHEN** the model passes `repeat` `daily` and `in_minutes` 60
- **THEN** the result is an error, and nothing is stored

### Requirement: Repeating reminders keep local time
Each occurrence of a repeating reminder SHALL be worked out from its rule and local time in the configured timezone, so it stays at the same local time across daylight saving changes. A local time that does not exist that day SHALL move forward by the gap; a time that occurs twice SHALL take the first.

#### Scenario: Across the autumn change
- **WHEN** a `daily` 08:00 reminder in `Europe/Lisbon` fires on Saturday 24 October 2026, and the clocks go back on Sunday 25 October
- **THEN** its next occurrence is Sunday 25 October at 08:00 Lisbon time

#### Scenario: In the spring gap
- **WHEN** a `daily` reminder `at` "01:30" falls on a day whose clocks jump from 01:00 to 02:00
- **THEN** that day's occurrence is at 02:30

### Requirement: Quiet reminders
`alfred_remind_set` SHALL take an optional `quiet_ok`, for reminders the user wants to hear about only when there is something to tell. In a quiet reminder's turn the model SHALL be told it may answer `NO_REPLY`, and a `NO_REPLY` answer SHALL send nothing and record nothing in the conversation. Without `quiet_ok`, `NO_REPLY` SHALL NOT be offered.

#### Scenario: Nothing downloading
- **WHEN** "tell me what's downloading, only if something is" fires with `quiet_ok` and the model answers `NO_REPLY`
- **THEN** the recipient receives nothing, and nothing is added to their conversation

#### Scenario: Something downloading
- **WHEN** the same reminder fires and the model answers "Dune is at 40 %"
- **THEN** the recipient receives that message, recorded as for any reminder

#### Scenario: One-shot quiet reminder
- **WHEN** the model sets "at 18:00, tell me if Dune finished downloading" with `quiet_ok`
- **THEN** that one-shot reminder's turn may answer `NO_REPLY` too

### Requirement: Reminder turns may use tools
A reminder turn SHALL be told that when the reminder asks it to find out, check or summarise something, it does so with the tools offered to the recipient before writing the message. It SHALL offer `alfred_reminders_list` when the recipient's allow-list matches it, and SHALL NOT offer `alfred_remind_set` or `alfred_reminder_cancel`.

#### Scenario: Morning briefing
- **WHEN** "brief me on the weather, what's downloading and my reminders for today" fires for `diogo`
- **THEN** the model can call the weather and download tools and `alfred_reminders_list` with `diogo`'s allow-list, and `diogo` receives the briefing

#### Scenario: No reminders set from a reminder turn
- **WHEN** a reminder turn runs
- **THEN** `alfred_remind_set` and `alfred_reminder_cancel` are not offered

#### Scenario: Set by someone else
- **WHEN** `diogo` set a briefing for `ana`
- **THEN** her turn offers only the tools `ana`'s allow-list matches, and reads only `ana`'s reminders

## MODIFIED Requirements

### Requirement: Reminder tools in user turns
In turns started by a user's message, alfred SHALL offer `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel` when the user's allow-list matches them, handled by alfred itself. They SHALL NOT be offered in `/ask` turns. In reminder turns only `alfred_reminders_list` SHALL be offered.

#### Scenario: Owner turn
- **WHEN** a user whose allow-list includes `alfred_*` sends a message
- **THEN** the offered tools include `alfred_remind_set`, `alfred_reminders_list` and `alfred_reminder_cancel`

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** none of the reminder tools are offered

#### Scenario: Reminder turn
- **WHEN** a reminder turn runs for a user whose allow-list includes `alfred_*`
- **THEN** `alfred_reminders_list` is offered, and `alfred_remind_set` and `alfred_reminder_cancel` are not

### Requirement: Listing reminders
`alfred_reminders_list` SHALL return the reminders that are set and not yet due on which the turn's user is the creator or a recipient, soonest first, each with its number, text, resolved time, how far away it is, and, when it is not the turn's user's alone, its creator and recipients. A repeating reminder SHALL be listed once, by its next time, with its rule; a quiet one SHALL say so. With none set it SHALL say so.

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

#### Scenario: Repeating reminder
- **WHEN** #31 "brief me" repeats every weekday at 08:00 with `quiet_ok`, and it is Sunday 15 November at 20:00
- **THEN** the list shows #31 once, at Monday 16 November 08:00, with "every weekday at 08:00" and that it may stay quiet

#### Scenario: Repeating reminder after it fired
- **WHEN** #31 fired on Monday at 08:00 and the list is asked for at 09:00
- **THEN** #31 is still listed, at Tuesday 08:00

### Requirement: Cancelling a reminder
`alfred_reminder_cancel` SHALL take a reminder number. If the reminder is set and not yet due and the turn's user is its creator, it SHALL be cancelled for all its recipients. If the turn's user is a recipient but not the creator, only they SHALL be removed; when no recipient is left, the reminder SHALL be cancelled. The result SHALL give its text and time, and who it stays set for. An unknown number, a reminder the user is not involved in, or one already delivered or cancelled SHALL be an error, the uninvolved case worded as an unknown number.

#### Scenario: Cancel a set reminder
- **WHEN** the model cancels #12, which is set for 17:00
- **THEN** #12 is never delivered, and the result names "call mum" and 17:00

#### Scenario: A recipient cancels a shared reminder
- **WHEN** `ana` cancels #15, which `diogo` set for both of them
- **THEN** #15 is still delivered to `diogo` and not to `ana`, `ana`'s list no longer shows it, and the result says it stays set for `diogo`

#### Scenario: The creator cancels a shared reminder
- **WHEN** `diogo` cancels #15, which he set for both of them
- **THEN** #15 is delivered to neither

#### Scenario: The last recipient leaves
- **WHEN** `ana` cancels #14, which `diogo` set for her alone
- **THEN** #14 is cancelled and delivered to no one

#### Scenario: Not involved
- **WHEN** `diogo` cancels #16, which `ana` set for herself
- **THEN** the result is the same error as for an unknown number, and #16 stays set

#### Scenario: Already delivered
- **WHEN** the model cancels #9, which was delivered yesterday
- **THEN** the result is an error saying #9 was already delivered

#### Scenario: Stop a series
- **WHEN** the model cancels #31, which repeats every weekday
- **THEN** no later occurrence of #31 is delivered

### Requirement: Due reminders are queued
alfred SHALL check for due reminders on the clock, at the start of every minute and at the configured interval within it, without making any AI request while none is due. Each due reminder SHALL be put into the message queue as one reminder item per recipient, carrying the occurrence's due time, and in the same step marked delivered or, if it repeats, moved to its next occurrence, so each recipient's item is queued exactly once even if alfred stops at that moment. Several due reminders SHALL be queued in due-time order.

#### Scenario: Reminder comes due
- **WHEN** reminder #12 is due at 17:00 and the check interval is 30 seconds
- **THEN** at 17:00:00 a reminder item for #12 is put in the queue, and #12 no longer appears in the list

#### Scenario: Shared reminder comes due
- **WHEN** #15, for `diogo` and `ana`, comes due
- **THEN** two reminder items are queued, one for each

#### Scenario: Nothing due
- **WHEN** no reminder is due for a whole day
- **THEN** alfred sends no request to the AI for reminders that day

#### Scenario: Repeating reminder comes due
- **WHEN** #31, every weekday at 08:00, comes due on Friday 20 November
- **THEN** an item for Friday 08:00 is queued and #31 is next due Monday 23 November at 08:00

### Requirement: Missed reminders are sent late
Reminders that came due while alfred was not running SHALL be queued when it starts, however late, and SHALL NOT be dropped. A repeating reminder SHALL be queued once, for its most recent missed occurrence, and then moved to its next occurrence after the current time.

#### Scenario: alfred down at the due time
- **WHEN** #12 was due at 17:00 and alfred was down from 16:00 to 19:40
- **THEN** after alfred starts, #12 is queued and reaches the owner

#### Scenario: Days late
- **WHEN** #12 was due three days before alfred starts
- **THEN** #12 is still queued and reaches the owner

#### Scenario: Repeating reminder after a long outage
- **WHEN** #31, every weekday at 08:00, missed Monday to Wednesday because alfred was down until Wednesday 10:00
- **THEN** one item is queued, for Wednesday 08:00, and #31 is next due Thursday 08:00

### Requirement: Reminder turns
A reminder item SHALL run as an AI turn for its recipient, in their conversation, whose input is labelled as a reminder with its number, text, the occurrence's due time, the current time, its repeat rule when it repeats and, when the recipient did not set it, who did, so the model can say when it is late and who asked. The model SHALL be told not to mention the reminder's number. The model's answer SHALL be sent to that recipient. `NO_REPLY` SHALL be offered only for a quiet reminder.

#### Scenario: On time
- **WHEN** #12 "call mum" is processed at 17:00
- **THEN** the model receives a reminder input naming #12, "call mum", due 17:00 and the current time 17:00, and the recipient receives its answer

#### Scenario: Late
- **WHEN** #12 due at 17:00 is processed at 19:40
- **THEN** the model's input shows due 17:00 and now 19:40

#### Scenario: Set by someone else
- **WHEN** `diogo` set #14 "buy milk" for `ana`
- **THEN** `ana`'s reminder turn input says `diogo` set it, and her answer is sent only to her

#### Scenario: Repeating
- **WHEN** #31 "brief me", every weekday at 08:00, is processed on Tuesday at 08:00
- **THEN** the input shows due Tuesday 08:00 and the rule "every weekday at 08:00"

### Requirement: A reminder always reaches its recipients
If a recipient's reminder turn fails without a retry, or the model answers nothing, or answers `NO_REPLY` for a reminder that is not quiet, alfred SHALL send that recipient the fixed reminder notice with the reminder's text and due time instead. A reminder turn that waits for an unreachable AI SHALL be retried like any other item.

#### Scenario: Model error
- **WHEN** the model answers a reminder turn with HTTP 400
- **THEN** the recipient receives the fixed reminder notice naming "call mum" and 17:00

#### Scenario: Model says NO_REPLY
- **WHEN** the model answers `NO_REPLY` to a reminder that is not quiet
- **THEN** the recipient receives the fixed reminder notice instead

#### Scenario: Quiet reminder fails
- **WHEN** the turn of a quiet reminder fails with HTTP 400
- **THEN** the recipient receives the fixed reminder notice

#### Scenario: AI asleep
- **WHEN** #12 comes due while `ai.lan` is unreachable
- **THEN** the reminder item is retried with backoff and the recipient receives the model's answer once the AI answers
