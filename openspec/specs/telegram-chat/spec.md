# telegram-chat Specification

## Purpose

Defines how alfred talks to its owner over Telegram: who it accepts messages from, how messages reach the AI, and how replies come back in a form that reads well in a chat app.

## Requirements

### Requirement: Long polling
alfred SHALL receive Telegram updates by long polling the Bot API. It SHALL NOT need a public URL or an inbound connection from Telegram.

#### Scenario: No inbound exposure
- **WHEN** alfred runs on a host with no public address and no port forwarding
- **THEN** it still receives the owner's messages

### Requirement: Owner-only access
alfred SHALL accept messages only from the configured owner Telegram user ID, in that user's private chat with the bot. Messages from anyone else, or from group chats, SHALL be ignored without any reply and logged with the sender's user ID only.

#### Scenario: Message from the owner
- **WHEN** the owner sends "what's downloading?" in a private chat with the bot
- **THEN** the message is queued for the AI

#### Scenario: Message from a stranger
- **WHEN** a user whose ID is not the owner's messages the bot
- **THEN** the bot sends no reply, nothing is queued, and a log entry records the rejected user ID without the message text

#### Scenario: Bot added to a group
- **WHEN** the owner writes in a group chat that contains the bot
- **THEN** the message is ignored

### Requirement: No bot commands
alfred SHALL NOT define bot commands. Every text message from the owner, including one starting with `/`, SHALL be sent to the AI as written.

#### Scenario: Slash-prefixed text
- **WHEN** the owner sends "/start"
- **THEN** it is queued for the AI as the text "/start"

### Requirement: Text only
alfred SHALL handle text messages only. For any other message type from the owner (photo, voice, sticker, file), it SHALL reply with a short fixed notice that only text is supported, and SHALL NOT queue anything.

#### Scenario: Voice message
- **WHEN** the owner sends a voice message
- **THEN** alfred replies that only text messages are supported and the AI is not called

### Requirement: Reply to every owner message
For each owner message, alfred SHALL send the AI's final reply to the owner's chat once the turn completes. If the turn fails without a reply, alfred SHALL send a short failure notice instead, so no message goes unanswered.

#### Scenario: Normal reply
- **WHEN** the AI turn for an owner message produces a final answer
- **THEN** that answer is sent to the owner's chat

#### Scenario: Turn fails
- **WHEN** an AI turn ends with an error that is not retried
- **THEN** the owner receives a short message saying the request failed, without internal details such as stack traces or tokens

### Requirement: Telegram formatting
alfred SHALL convert the model's Markdown-style output into Telegram-supported formatting. If Telegram rejects the formatted message, alfred SHALL resend the same content as plain text.

#### Scenario: Bold and code
- **WHEN** the reply contains `**disk full**` and a fenced code block
- **THEN** the owner sees bold text and a monospace block in Telegram

#### Scenario: Formatting rejected
- **WHEN** Telegram rejects a formatted message as unparseable
- **THEN** the same text is sent without formatting and the owner still receives it

### Requirement: Long replies are split
A reply longer than Telegram's maximum message length SHALL be sent as several consecutive messages, split at paragraph or line boundaries where possible, with no content lost.

#### Scenario: Oversized reply
- **WHEN** a reply is 6000 characters long
- **THEN** it arrives as two or more messages in order, together containing the full reply

### Requirement: Typing indicator while an owner turn runs
While the AI turn for an owner message is running, alfred SHALL show the typing indicator in the owner's chat, renewing it so it stays visible for the whole turn, and SHALL stop renewing it when the turn ends. It SHALL NOT show the indicator for `/ask` turns, or while no turn is running (an item waiting in the queue or for a retry, or a summary being made after a reset).

#### Scenario: Owner turn running
- **WHEN** the owner sends "what's downloading?" and its AI turn runs for 20 seconds
- **THEN** the owner's chat shows "typing…" for those 20 seconds, until the reply arrives

#### Scenario: Turn ends
- **WHEN** an owner turn ends with a reply or a failure notice
- **THEN** alfred stops sending the typing indicator

#### Scenario: Waiting for a retry
- **WHEN** an owner message is waiting to be retried because the AI is unreachable
- **THEN** no typing indicator is sent during the wait, and it is shown again when the retried turn starts

#### Scenario: Waiting in the queue
- **WHEN** an owner message waits behind another item in the queue
- **THEN** no typing indicator is sent for it until its own turn starts

#### Scenario: Service ask
- **WHEN** an `/ask` turn runs
- **THEN** no typing indicator is sent to the owner's chat

### Requirement: Typing indicator failures don't affect the turn
If Telegram does not accept the typing indicator, alfred SHALL log it and keep trying for the rest of the turn. The failure SHALL NOT fail, delay or retry the turn, and SHALL NOT change its reply.

#### Scenario: Telegram rejects the typing action
- **WHEN** sending the typing indicator fails during an owner turn
- **THEN** the turn completes normally, the owner receives the reply, and the failure is logged

#### Scenario: Telegram recovers mid-turn
- **WHEN** one typing indicator request fails and later requests in the same turn succeed
- **THEN** the typing indicator is shown again for the rest of the turn
