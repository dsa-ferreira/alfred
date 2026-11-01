# Spec Delta

## Purpose

Defines how alfred talks to its owner over Telegram: who it accepts messages from, how messages reach the AI, and how replies come back in a form that reads well in a chat app.

## ADDED Requirements

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
