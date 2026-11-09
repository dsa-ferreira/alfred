# Spec Delta

## MODIFIED Requirements

### Requirement: One current conversation
alfred SHALL keep exactly one current conversation with the owner, stored durably. Owner messages, the AI's replies to them, `/notify` notices, `/ask` turns that notified the owner and delivered reminders SHALL be recorded in it in the order they happened.

#### Scenario: History survives restart
- **WHEN** alfred restarts in the middle of a conversation
- **THEN** the next turn's context still contains the messages from before the restart

## ADDED Requirements

### Requirement: Reminders in the conversation
A delivered reminder SHALL be recorded in the current conversation as two entries: the reminder, with its number, text and due time, and the message the owner received (the model's answer or the fixed reminder notice). The model SHALL see the reminder in later turns' context labelled as a reminder with its number and due time.

#### Scenario: Reply to a reminder
- **WHEN** reminder #12 "call mum", due 17:00, was delivered and the owner then says "remind me again in an hour"
- **THEN** the model's context for that turn includes a message labelled as reminder #12 due at 17:00 with the text "call mum", followed by what the owner was told

#### Scenario: Reminder set in an archived conversation
- **WHEN** #12 was set in conversation #41, which has since been archived, and #12 is delivered during conversation #42
- **THEN** #12 and the message the owner received are recorded in #42
