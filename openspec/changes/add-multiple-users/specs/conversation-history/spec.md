# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: One current conversation`
- TO: `### Requirement: One current conversation per user`

## MODIFIED Requirements

### Requirement: One current conversation per user
alfred SHALL keep exactly one current conversation for each user, stored durably. A user's messages, the AI's replies to them, `/notify` notices sent to that user, `/ask` turns that notified that user and reminders delivered to that user SHALL be recorded in that user's current conversation in the order they happened. A turn's context SHALL come only from its user's conversation.

#### Scenario: History survives restart
- **WHEN** alfred restarts in the middle of a conversation
- **THEN** the next turn's context still contains that user's messages from before the restart

#### Scenario: Separate conversations
- **WHEN** `diogo` asked about a recipe and `ana` then asks "what was that recipe?"
- **THEN** `ana`'s turn context contains none of `diogo`'s messages

### Requirement: Notices are part of the conversation
A delivered `/notify` notice SHALL be recorded in the target user's conversation as a notice from the named service. The model SHALL see it in that user's later turns' context, labelled with the service name.

#### Scenario: Reply to a notice
- **WHEN** `backup` notifies the admin "Nightly backup failed: disk full" and the admin then asks "what's eating the space?"
- **THEN** the model's context for that turn includes the backup notice labelled as coming from `backup`

#### Scenario: Notice for another user
- **WHEN** `mcparr` notifies `ana` "Dune finished"
- **THEN** the notice is recorded in `ana`'s conversation and not in the admin's

### Requirement: Ask turns in the conversation
An `/ask` turn whose answer was sent to its target user SHALL be recorded in that user's conversation: the service's prompt, and the answer the user received. An `/ask` turn answered with `NO_REPLY` SHALL NOT be recorded in any conversation.

#### Scenario: Silent ask
- **WHEN** an `/ask` turn ends with `NO_REPLY`
- **THEN** the next turn's context does not contain that prompt or answer

#### Scenario: Notifying ask
- **WHEN** an `/ask` turn from `disk-check` sends the admin a warning
- **THEN** the admin's later turns' context includes the `disk-check` prompt and the warning

### Requirement: End a conversation in natural language
In turns started by a user's message, the model SHALL be offered `alfred_end_conversation` when the user's allow-list matches it, described as for use only when the user clearly says the conversation or topic is finished. alfred SHALL NOT offer it in `/ask` turns.

#### Scenario: Owner wraps up
- **WHEN** a user whose allow-list matches it says "great, that's sorted, we're done here"
- **THEN** the model can call `alfred_end_conversation`

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** `alfred_end_conversation` is not among the offered tools

### Requirement: Reset after the turn
When `alfred_end_conversation` is called, alfred SHALL finish the turn, send the reply, then archive that user's current conversation and start a new one for them. It SHALL then summarise the archived conversation and send that user a marker naming the archived conversation's id and title and the new conversation's id, for example `— #41 "Chicken thighs recipe" —` and `— new conversation #42 —`. If the summary cannot be made, the marker SHALL name only the ids. Entries for that user that arrive after the archive SHALL belong to the new conversation. Other users' conversations SHALL be unaffected.

#### Scenario: Reset sequence
- **WHEN** the model calls `alfred_end_conversation` in conversation #41 and replies "Done 👍"
- **THEN** the user receives "Done 👍" followed by a marker with `#41`, its title and `new conversation #42`, and their next message's context contains none of the earlier entries

#### Scenario: Summary fails at reset
- **WHEN** the summary request fails while conversation #41 is being archived
- **THEN** the user still receives a marker naming `#41` and `new conversation #42`, without a title

#### Scenario: Notice after reset
- **WHEN** a `/notify` notice for that user arrives after a reset
- **THEN** it is recorded in their new conversation

#### Scenario: Other user unaffected
- **WHEN** `ana` ends her conversation
- **THEN** `diogo`'s current conversation stays current and unchanged

### Requirement: Reminders in the conversation
A delivered reminder SHALL be recorded in each recipient's current conversation as two entries: the reminder, with its number, text, due time and, when the recipient did not set it, who did; and the message that recipient received (the model's answer or the fixed reminder notice). The model SHALL see the reminder in that recipient's later turns' context labelled as a reminder with its number and due time.

#### Scenario: Reply to a reminder
- **WHEN** reminder #12 "call mum", due 17:00, was delivered and the user then says "remind me again in an hour"
- **THEN** the model's context for that turn includes a message labelled as reminder #12 due at 17:00 with the text "call mum", followed by what the user was told

#### Scenario: Reminder set in an archived conversation
- **WHEN** #12 was set in conversation #41, which has since been archived, and #12 is delivered during conversation #42
- **THEN** #12 and the message the user received are recorded in #42

#### Scenario: Shared reminder
- **WHEN** #15 has recipients `diogo` and `ana`
- **THEN** #15 is recorded in each one's current conversation, each followed by what that user was told
