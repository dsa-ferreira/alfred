## MODIFIED Requirements

### Requirement: Reset after the turn
When `alfred_end_conversation` is called, alfred SHALL finish the turn, send the reply, then archive that user's current conversation and start a new one for them. It SHALL then send that user a marker naming the archived conversation's id and the new conversation's id, `— #41 archived —` and `— new conversation #42 —`, without waiting for the archived conversation's summary. Entries for that user that arrive after the archive SHALL belong to the new conversation. Other users' conversations SHALL be unaffected.

#### Scenario: Reset sequence
- **WHEN** the model calls `alfred_end_conversation` in conversation #41 and replies "Done 👍"
- **THEN** the user receives "Done 👍" followed by a marker with `#41 archived` and `new conversation #42`, and their next message's context contains none of the earlier entries

#### Scenario: Next message right after a reset
- **WHEN** the user ends conversation #41 and immediately sends "new topic: what's for dinner?"
- **THEN** that message's turn starts without waiting for #41 to be summarised

#### Scenario: Summary fails at reset
- **WHEN** the model server is unreachable while conversation #41 is being ended
- **THEN** the user still receives the marker naming `#41 archived` and `new conversation #42`, and #41 is summarised later

#### Scenario: Notice after reset
- **WHEN** a `/notify` notice for that user arrives after a reset
- **THEN** it is recorded in their new conversation

#### Scenario: Other user unaffected
- **WHEN** `ana` ends her conversation
- **THEN** `diogo`'s current conversation stays current and unchanged

## ADDED Requirements

### Requirement: Idle conversations close
When the idle close time is set, alfred SHALL archive a user's current conversation and start a new one for them once that time has passed since the user's last own message in it. Notices, `/ask` turns and reminders SHALL NOT count. A conversation with no message from its user SHALL NOT be closed this way. Nothing SHALL be sent to the user at that moment.

#### Scenario: Idle overnight
- **WHEN** the idle close time is 6 hours and the user's last message in #41 was at 23:10
- **THEN** at about 05:10 #41 is archived and #42 becomes the user's current conversation, and the user is sent nothing

#### Scenario: Notices don't keep it open
- **WHEN** the idle close time is 6 hours, the user last wrote at 23:10, and `backup` notified them at 02:00
- **THEN** the conversation is still closed at about 05:10, and the notice stays in the archived conversation

#### Scenario: Only notices
- **WHEN** the user's current conversation contains only notices and reminders, however old
- **THEN** it is not closed for inactivity

#### Scenario: Idle close off
- **WHEN** the idle close time is not set
- **THEN** conversations are only ended by `alfred_end_conversation`

#### Scenario: Other users
- **WHEN** `ana`'s conversation is closed for inactivity
- **THEN** `diogo`'s current conversation stays current and unchanged

### Requirement: No idle close during a turn
alfred SHALL NOT close a user's conversation for inactivity while one of that user's queue items is waiting or running.

#### Scenario: Message waiting in the queue
- **WHEN** the idle close time has just passed for `ana`, and `ana`'s new message is waiting behind another item in the queue
- **THEN** her conversation is not closed, and her message is answered in it

### Requirement: Idle close marker on the next message
After a conversation was closed for inactivity, alfred SHALL send its user the marker `— #41 closed after inactivity —` and `— new conversation #42 —` when that user's next message starts its turn, before the reply, and only once.

#### Scenario: Next morning
- **WHEN** #41 was closed for inactivity overnight and the user writes "morning! what's the weather?"
- **THEN** the user receives the marker naming `#41` and `new conversation #42`, then the reply, and the turn's context contains none of #41's entries

#### Scenario: Marker sent once
- **WHEN** the user sends a second message after that
- **THEN** no marker is sent again

#### Scenario: Notices are not a next message
- **WHEN** #41 was closed for inactivity and a `/notify` notice then reaches the user
- **THEN** no marker is sent with the notice
