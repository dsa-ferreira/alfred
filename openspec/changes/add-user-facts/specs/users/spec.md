# Spec Delta

## MODIFIED Requirements

### Requirement: Users don't see each other's data
A user's turns and commands SHALL NOT see another user's conversations, history, archived conversations, attachments, facts or reminders, except reminders on which that user is the creator or a recipient.

#### Scenario: Searching memory
- **WHEN** `ana` asks "what did we talk about yesterday?"
- **THEN** only `ana`'s archived conversations can be returned

#### Scenario: Facts
- **WHEN** `diogo` has facts and `ana`'s turn runs, or `ana` sends `/memory`
- **THEN** none of `diogo`'s facts are in `ana`'s prompt, tool results or `/memory` replies
