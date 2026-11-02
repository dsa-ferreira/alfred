# Spec Delta

## MODIFIED Requirements

### Requirement: Bounded context window
The model SHALL receive at most the configured number of most recent entries (owner messages, replies, notices and recalls) from the current conversation. Tool calls and tool results from earlier turns SHALL NOT be replayed. A recall entry SHALL count as one entry. Only the newest recall entry in the window SHALL be sent with its transcript; older recall entries in the window SHALL be sent as the recalled conversation's title and summary.

#### Scenario: Long conversation
- **WHEN** the window is 30 and the current conversation has 50 entries
- **THEN** the model receives only the 30 most recent entries

#### Scenario: Two recalls in the window
- **WHEN** #41 and then #57 were recalled into the current conversation and both are within the window
- **THEN** the model receives #57's transcript, and #41 only as its title and summary

### Requirement: Reset after the turn
When `alfred_end_conversation` is called, alfred SHALL finish the turn, send the reply, then archive the current conversation and start a new one. It SHALL then summarise the archived conversation and send a marker to the owner naming the archived conversation's id and title and the new conversation's id, for example `— #41 "Chicken thighs recipe" —` and `— new conversation #42 —`. If the summary cannot be made, the marker SHALL name only the ids. Entries that arrive after the archive SHALL belong to the new conversation.

#### Scenario: Reset sequence
- **WHEN** the model calls `alfred_end_conversation` in conversation #41 and replies "Done 👍"
- **THEN** the owner receives "Done 👍" followed by a marker with `#41`, its title and `new conversation #42`, and the next owner message's context contains none of the earlier entries

#### Scenario: Summary fails at reset
- **WHEN** the summary request fails while conversation #41 is being archived
- **THEN** the owner still receives a marker naming `#41` and `new conversation #42`, without a title

#### Scenario: Notice after reset
- **WHEN** a `/notify` notice arrives after a reset
- **THEN** it is recorded in the new conversation

### Requirement: Archived conversations are kept
Archived conversations SHALL remain stored. They SHALL NOT be sent to the model except through a search result or a recall.

#### Scenario: Archive retained
- **WHEN** a conversation is ended
- **THEN** its entries remain in the database, marked as belonging to an archived conversation

#### Scenario: Not sent unless recalled
- **WHEN** conversation #41 is archived and nothing has searched or recalled it
- **THEN** none of its entries are in the model's context
