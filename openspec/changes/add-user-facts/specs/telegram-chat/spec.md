# Spec Delta

## MODIFIED Requirements

### Requirement: No bot commands
alfred SHALL define exactly one bot command, `/memory` (see `user-facts`). Every other text message from the owner, including one starting with `/`, SHALL be sent to the AI as written.

#### Scenario: Slash-prefixed text
- **WHEN** the owner sends "/start"
- **THEN** it is queued for the AI as the text "/start"

#### Scenario: Memory command
- **WHEN** the owner sends "/memory"
- **THEN** it is answered without the AI and is not queued

#### Scenario: Text that only starts like the command
- **WHEN** the owner sends "/memorybank ideas"
- **THEN** it is queued for the AI as written
