# Spec Delta

## MODIFIED Requirements

### Requirement: Reply to every owner message
For each user's message, alfred SHALL send the AI's final reply to that user's chat once the turn completes. If the turn fails without a reply, alfred SHALL send a short failure notice to that chat instead, so no message goes unanswered. Nothing from a user's turn SHALL be sent to another user's chat. A turn that waits for the user's answer (see `user-input-requests`) SHALL send its reply when it resumes and ends. A waiting turn cancelled by a new message or by expiry SHALL get no reply: its question message says what happened.

#### Scenario: Normal reply
- **WHEN** the AI turn for `ana`'s message produces a final answer
- **THEN** that answer is sent to `ana`'s chat and not to anyone else's

#### Scenario: Turn fails
- **WHEN** an AI turn ends with an error that is not retried
- **THEN** the user who sent the message receives a short message saying the request failed, without internal details such as stack traces or tokens

#### Scenario: Reply after confirmation
- **WHEN** `ana`'s turn waits for her confirmation and she taps **Do it**
- **THEN** she gets the turn's reply after the tool has run

#### Scenario: Cancelled while waiting
- **WHEN** `ana`'s waiting turn is cancelled because she sent a new message
- **THEN** no reply or failure notice is sent for it

### Requirement: Typing indicator while an owner turn runs
While the AI turn for a user's message is running, alfred SHALL show the typing indicator in that user's chat, renewing it so it stays visible for the whole turn, and SHALL stop renewing it when the turn ends. It SHALL NOT show the indicator for `/ask` turns, or while no turn is running (an item waiting in the queue, for a retry or for the user's answer, or a summary being made after a reset). A resumed turn started by a user's message SHALL show it again until it ends.

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

#### Scenario: Waiting for the user's answer
- **WHEN** `ana`'s turn waits for her confirmation
- **THEN** no typing indicator is sent while it waits, and it is shown again from her tap until the turn's reply

## ADDED Requirements

### Requirement: Buttons
alfred SHALL receive button taps (callback queries) on its own messages along with messages. Every tap SHALL be acknowledged to Telegram, so the client stops showing it as loading, with a short text when the tap had no effect. Taps from anyone other than a configured user in their private chat SHALL be ignored without effect and logged with the sender's user ID only.

#### Scenario: Tap acknowledged
- **WHEN** `ana` taps a button on alfred's question message
- **THEN** Telegram is told the tap was received, and her client stops waiting

#### Scenario: Tap from a stranger
- **WHEN** a callback query arrives from a Telegram user who is not configured
- **THEN** nothing happens, and a log entry records only that user's ID
