# Spec Delta

## ADDED Requirements

### Requirement: Conversations without user messages close when something arrives
When the idle close time is set and a notice, `/ask` turn or reminder is about to be recorded in a user's current conversation that has no message from that user and whose newest entry is older than the idle close time, alfred SHALL first archive that conversation and start a new one for the user, and record the new entries there. No marker SHALL be sent for it, then or later.

#### Scenario: Unanswered daily briefing
- **WHEN** the idle close time is 6 hours, `diogo`'s conversation #42 holds only Tuesday's 08:00 briefing, and Wednesday's 08:00 briefing is delivered
- **THEN** #42 is archived, Wednesday's briefing and what `diogo` was told are recorded in a new conversation #43, and no marker is sent

#### Scenario: Late reply keeps the briefing
- **WHEN** `diogo`'s conversation holds only the 08:00 briefing and he writes "more about the second download?" at 15:00
- **THEN** his message is recorded in that conversation, and the turn's context contains the briefing

#### Scenario: Burst of notices
- **WHEN** the idle close time is 6 hours and `backup` notifies `diogo` at 02:00 and again at 02:10, in a conversation with no message from him
- **THEN** both notices are in the same conversation

#### Scenario: Conversation with a user message
- **WHEN** the user last wrote 3 hours ago, and a reminder is delivered
- **THEN** it is recorded in that conversation, and the conversation closes as for "Idle conversations close"

#### Scenario: Marker for an earlier conversation
- **WHEN** #41 was closed for inactivity, #42 then held only a briefing and was archived when the next one arrived in #43, and the user then writes
- **THEN** the user receives the marker naming `#41` and `new conversation #43`, once

#### Scenario: Idle close off
- **WHEN** the idle close time is not set
- **THEN** notices and reminders are recorded in the current conversation however old its newest entry is

## MODIFIED Requirements

### Requirement: Reminders in the conversation
A delivered reminder SHALL be recorded in each recipient's current conversation as two entries: the reminder, with its number, text, the due time of the occurrence delivered and, when the recipient did not set it, who did; and the message that recipient received (the model's answer or the fixed reminder notice). The model SHALL see the reminder in that recipient's later turns' context labelled as a reminder with its number and that due time. A quiet reminder answered with `NO_REPLY` SHALL NOT be recorded.

#### Scenario: Reply to a reminder
- **WHEN** reminder #12 "call mum", due 17:00, was delivered and the user then says "remind me again in an hour"
- **THEN** the model's context for that turn includes a message labelled as reminder #12 due at 17:00 with the text "call mum", followed by what the user was told

#### Scenario: Reminder set in an archived conversation
- **WHEN** #12 was set in conversation #41, which has since been archived, and #12 is delivered during conversation #42
- **THEN** #12 and the message the user received are recorded in #42

#### Scenario: Shared reminder
- **WHEN** #15 has recipients `diogo` and `ana`
- **THEN** #15 is recorded in each one's current conversation, each followed by what that user was told

#### Scenario: Repeating reminder in the history
- **WHEN** #31, every weekday at 08:00, was delivered on Monday and Tuesday, and both are still in the window on Tuesday at 09:00
- **THEN** the model sees one labelled due Monday 08:00 and one due Tuesday 08:00, not the series' next time

### Requirement: Idle conversations close
When the idle close time is set, alfred SHALL archive a user's current conversation and start a new one for them once that time has passed since the user's last own message in it. Notices, `/ask` turns and reminders SHALL NOT count. A conversation with no message from its user SHALL NOT be closed this way; it closes only as in "Conversations without user messages close when something arrives". Nothing SHALL be sent to the user at that moment.

#### Scenario: Idle overnight
- **WHEN** the idle close time is 6 hours and the user's last message in #41 was at 23:10
- **THEN** at about 05:10 #41 is archived and #42 becomes the user's current conversation, and the user is sent nothing

#### Scenario: Notices don't keep it open
- **WHEN** the idle close time is 6 hours, the user last wrote at 23:10, and `backup` notified them at 02:00
- **THEN** the conversation is still closed at about 05:10, and the notice stays in the archived conversation

#### Scenario: Only notices
- **WHEN** the user's current conversation contains only notices and reminders, however old, and nothing new arrives
- **THEN** it is not closed for inactivity

#### Scenario: Idle close off
- **WHEN** the idle close time is not set
- **THEN** conversations are only ended by `alfred_end_conversation`

#### Scenario: Other users
- **WHEN** `ana`'s conversation is closed for inactivity
- **THEN** `diogo`'s current conversation stays current and unchanged
