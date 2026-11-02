# Spec Delta

## ADDED Requirements

### Requirement: Summary request token counts are logged
After each successful summary request, alfred SHALL log at INFO level the prompt and completion token counts the model server reported, with the conversation's id. If the reply has no token counts, the line SHALL say they were not reported, and the summary SHALL be stored normally.

#### Scenario: Conversation summarised
- **WHEN** conversation #41 is summarised and the server reports 3120 prompt tokens and 58 completion tokens
- **THEN** the log has a line for conversation #41 with 3120 prompt tokens and 58 completion tokens

#### Scenario: Server without token counts
- **WHEN** the summary reply has no token counts
- **THEN** the log line says they were not reported and the title and summary are stored
