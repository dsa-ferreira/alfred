# Spec Delta

## MODIFIED Requirements

### Requirement: Telegram-oriented system prompt
Every turn SHALL include a system prompt made of a persona, which sets alfred's voice and is configurable with a built-in default, followed by built-in operating rules that cannot be configured, followed by the instructions of the turn's available MCP servers. The operating rules SHALL tell the model it is chatting with its owner over Telegram, how to use tools, and how to read service notices, `/ask` messages and recalled conversations.

#### Scenario: Default prompt
- **WHEN** no custom persona is configured
- **THEN** the system prompt contains the built-in persona followed by the built-in operating rules

#### Scenario: Custom prompt
- **WHEN** a custom persona is configured
- **THEN** it replaces only the built-in persona, and the built-in operating rules are still sent after it

#### Scenario: Operating rules survive a custom persona
- **WHEN** a custom persona is configured and the owner asks about an earlier `[notice:backup]` message
- **THEN** the system prompt still explains that `[notice:<service>]` messages are notifications the owner has already seen

#### Scenario: Recalled conversation explained
- **WHEN** the current conversation contains a recall of conversation #41
- **THEN** the system prompt explains that a `[recall:#<id> …]` message is an earlier conversation brought back on the owner's request, not something the owner just said

#### Scenario: Server instructions after the rules
- **WHEN** cookbook is available and sends instructions, and the turn is an `/ask` turn
- **THEN** the system prompt contains the persona, then the operating rules, then cookbook's instructions, then the `NO_REPLY` instruction

#### Scenario: Server instructions survive a custom persona
- **WHEN** a custom persona is configured and cookbook sends instructions
- **THEN** cookbook's instructions are still in the system prompt

## ADDED Requirements

### Requirement: Current date in the system prompt
The system prompt of every turn, owner and `/ask`, SHALL state the current date and weekday in the configured timezone. It SHALL NOT include the time of day, so the prompt changes at most once a day.

#### Scenario: Owner turn
- **WHEN** the owner sends a message on Monday 2 November 2026 at 14:05 Lisbon time, with the timezone set to `Europe/Lisbon`
- **THEN** the system prompt says that today is Monday, 2 November 2026, and contains no time of day

#### Scenario: Date changes at local midnight
- **WHEN** the timezone is `Europe/Lisbon` and a turn runs at 00:30 Lisbon time on 3 November 2026 (still 2 November in some other zones)
- **THEN** the system prompt says Tuesday, 3 November 2026

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** its system prompt also states the current date
