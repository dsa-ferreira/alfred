# Spec Delta

## MODIFIED Requirements

### Requirement: Telegram-oriented system prompt
Every turn SHALL include a system prompt made of two parts: a persona, which sets alfred's voice and is configurable with a built-in default, followed by built-in operating rules that cannot be configured. The operating rules SHALL tell the model it is chatting with its owner over Telegram, how to use tools, and how to read service notices and `/ask` messages.

#### Scenario: Default prompt
- **WHEN** no custom persona is configured
- **THEN** the system prompt contains the built-in persona followed by the built-in operating rules

#### Scenario: Custom prompt
- **WHEN** a custom persona is configured
- **THEN** it replaces only the built-in persona, and the built-in operating rules are still sent after it

#### Scenario: Operating rules survive a custom persona
- **WHEN** a custom persona is configured and the owner asks about an earlier `[notice:backup]` message
- **THEN** the system prompt still explains that `[notice:<service>]` messages are notifications the owner has already seen
