# Spec Delta

## MODIFIED Requirements

### Requirement: Single ordered worker
Owner messages, `/ask` prompts and due reminders SHALL go into one shared queue and SHALL be processed one at a time in arrival order. No two AI turns SHALL run at the same time. A turn waiting for its user's answer (see `user-input-requests`) SHALL NOT hold up the queue. When it is answered it SHALL resume before any item that arrived after it, once the turn running at that moment has finished.

#### Scenario: Two messages in quick succession
- **WHEN** the owner sends "add Dune Part Three" and then "and what's downloading?" before the first turn finishes
- **THEN** the second message is processed only after the first turn completes, and it sees the first turn in its context

#### Scenario: Owner message and ask interleaved
- **WHEN** an `/ask` arrives while an owner message is being processed
- **THEN** the `/ask` turn starts after the owner's turn finishes

#### Scenario: Reminder during an owner turn
- **WHEN** a reminder comes due while an owner message is being processed
- **THEN** the reminder turn starts after the owner's turn finishes, and sees that turn in its context

#### Scenario: Waiting turn doesn't block
- **WHEN** `ana`'s turn waits for her confirmation and an `/ask` for `diogo` arrives
- **THEN** the `/ask` turn runs without waiting for `ana`'s answer

#### Scenario: Answered turn goes first
- **WHEN** `ana` confirms while `diogo`'s turn runs and a reminder for `diogo` is queued behind it
- **THEN** `ana`'s turn resumes after `diogo`'s turn ends and before the reminder

## ADDED Requirements

### Requirement: Waiting items are kept
A waiting item SHALL stay stored, with its turn's progress, until it is answered, cancelled or expires, including across restarts. A resumed turn SHALL count as one in which tools already ran: if the AI becomes unreachable after it resumes, it SHALL NOT be retried.

#### Scenario: AI down after the answer
- **WHEN** `ana` confirms, the confirmed tool runs, and the next model request times out
- **THEN** the turn is not retried, and `ana` gets the notice that the request may have partly completed
