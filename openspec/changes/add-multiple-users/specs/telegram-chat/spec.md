# Spec Delta

## RENAMED Requirements

- FROM: `### Requirement: Owner-only access`
- TO: `### Requirement: Configured users only`

## MODIFIED Requirements

### Requirement: Configured users only
alfred SHALL accept messages only from configured users' Telegram user IDs, each in that user's private chat with the bot. A message SHALL be queued as that user's. Messages from anyone else, or from group chats, SHALL be ignored without any reply and logged with the sender's user ID only.

#### Scenario: Message from the owner
- **WHEN** configured user `ana` sends "what's for dinner?" in her private chat with the bot
- **THEN** the message is queued for the AI as `ana`'s

#### Scenario: Message from a stranger
- **WHEN** a user whose ID is not configured messages the bot
- **THEN** the bot sends no reply, nothing is queued, and a log entry records the rejected user ID without the message text

#### Scenario: Bot added to a group
- **WHEN** a configured user writes in a group chat that contains the bot
- **THEN** the message is ignored

### Requirement: Reply to every owner message
For each user's message, alfred SHALL send the AI's final reply to that user's chat once the turn completes. If the turn fails without a reply, alfred SHALL send a short failure notice to that chat instead, so no message goes unanswered. Nothing from a user's turn SHALL be sent to another user's chat.

#### Scenario: Normal reply
- **WHEN** the AI turn for `ana`'s message produces a final answer
- **THEN** that answer is sent to `ana`'s chat and not to anyone else's

#### Scenario: Turn fails
- **WHEN** an AI turn ends with an error that is not retried
- **THEN** the user who sent the message receives a short message saying the request failed, without internal details such as stack traces or tokens

### Requirement: Typing indicator while an owner turn runs
While the AI turn for a user's message is running, alfred SHALL show the typing indicator in that user's chat, renewing it so it stays visible for the whole turn, and SHALL stop renewing it when the turn ends. It SHALL NOT show the indicator for `/ask` turns, or while no turn is running (an item waiting in the queue or for a retry, or a summary being made after a reset).

#### Scenario: Owner turn running
- **WHEN** `ana` sends "what's downloading?" and its AI turn runs for 20 seconds
- **THEN** `ana`'s chat shows "typing…" for those 20 seconds, until the reply arrives, and no other chat does

#### Scenario: Turn ends
- **WHEN** a user's turn ends with a reply or a failure notice
- **THEN** alfred stops sending the typing indicator

#### Scenario: Waiting for a retry
- **WHEN** a user's message is waiting to be retried because the AI is unreachable
- **THEN** no typing indicator is sent during the wait, and it is shown again when the retried turn starts

#### Scenario: Waiting in the queue
- **WHEN** `ana`'s message waits behind `diogo`'s turn in the queue
- **THEN** no typing indicator is sent to `ana`'s chat until her own turn starts

#### Scenario: Service ask
- **WHEN** an `/ask` turn runs
- **THEN** no typing indicator is sent to any chat
