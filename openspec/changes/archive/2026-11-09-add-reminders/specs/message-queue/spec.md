# Spec Delta

## MODIFIED Requirements

### Requirement: Single ordered worker
Owner messages, `/ask` prompts and due reminders SHALL go into one shared queue and SHALL be processed one at a time in arrival order. No two AI turns SHALL run at the same time.

#### Scenario: Two messages in quick succession
- **WHEN** the owner sends "add Dune Part Three" and then "and what's downloading?" before the first turn finishes
- **THEN** the second message is processed only after the first turn completes, and it sees the first turn in its context

#### Scenario: Owner message and ask interleaved
- **WHEN** an `/ask` arrives while an owner message is being processed
- **THEN** the `/ask` turn starts after the owner's turn finishes

#### Scenario: Reminder during an owner turn
- **WHEN** a reminder comes due while an owner message is being processed
- **THEN** the reminder turn starts after the owner's turn finishes, and sees that turn in its context
