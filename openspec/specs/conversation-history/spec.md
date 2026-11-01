# conversation-history Specification

## Purpose

Defines what alfred remembers between messages: one current conversation with the owner, which includes service notices, a bounded window of it given to the model, and a natural-language way to end the conversation and start fresh.

## Requirements

### Requirement: One current conversation
alfred SHALL keep exactly one current conversation with the owner, stored durably. Owner messages, the AI's replies to them, `/notify` notices and `/ask` turns that notified the owner SHALL be recorded in it in the order they happened.

#### Scenario: History survives restart
- **WHEN** alfred restarts in the middle of a conversation
- **THEN** the next turn's context still contains the messages from before the restart

### Requirement: Notices are part of the conversation
A delivered `/notify` notice SHALL be recorded as a notice from the named service. The model SHALL see it in later turns' context, labelled with the service name.

#### Scenario: Reply to a notice
- **WHEN** `backup` notifies "Nightly backup failed: disk full" and the owner then asks "what's eating the space?"
- **THEN** the model's context for that turn includes the backup notice labelled as coming from `backup`

### Requirement: Ask turns in the conversation
An `/ask` turn whose answer was sent to the owner SHALL be recorded in the conversation: the service's prompt, and the answer the owner received. An `/ask` turn answered with `NO_REPLY` SHALL NOT be recorded in the conversation.

#### Scenario: Silent ask
- **WHEN** an `/ask` turn ends with `NO_REPLY`
- **THEN** the next turn's context does not contain that prompt or answer

#### Scenario: Notifying ask
- **WHEN** an `/ask` turn from `disk-check` sends the owner a warning
- **THEN** later turns' context includes the `disk-check` prompt and the warning

### Requirement: Bounded context window
The model SHALL receive at most the configured number of most recent entries (owner messages, replies and notices) from the current conversation. Tool calls and tool results from earlier turns SHALL NOT be replayed.

#### Scenario: Long conversation
- **WHEN** the window is 30 and the current conversation has 50 entries
- **THEN** the model receives only the 30 most recent entries

### Requirement: End a conversation in natural language
In turns started by an owner message, the model SHALL be offered `alfred_end_conversation`, described as for use only when the owner clearly says the conversation or topic is finished. alfred SHALL NOT offer it in `/ask` turns.

#### Scenario: Owner wraps up
- **WHEN** the owner says "great, that's sorted, we're done here"
- **THEN** the model can call `alfred_end_conversation`

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** `alfred_end_conversation` is not among the offered tools

### Requirement: Reset after the turn
When `alfred_end_conversation` is called, alfred SHALL finish the turn, send the reply, then archive the current conversation and start a new one. It SHALL then send the marker `— new conversation —` to the owner. The next entry SHALL begin the new conversation.

#### Scenario: Reset sequence
- **WHEN** the model calls `alfred_end_conversation` and replies "Done 👍"
- **THEN** the owner receives "Done 👍" followed by `— new conversation —`, and the next owner message's context contains none of the earlier entries

#### Scenario: Notice after reset
- **WHEN** a `/notify` notice arrives after a reset
- **THEN** it is recorded in the new conversation

### Requirement: Archived conversations are kept
Archived conversations SHALL remain stored and SHALL NOT be sent to the model.

#### Scenario: Archive retained
- **WHEN** a conversation is ended
- **THEN** its entries remain in the database, marked as belonging to an archived conversation
