# Spec Delta

## Purpose

Defines how messages bound for the AI (owner messages and `/ask` prompts) are processed: one at a time, in arrival order, without being lost when the AI or alfred is down.

## ADDED Requirements

### Requirement: Single ordered worker
Owner messages and `/ask` prompts SHALL go into one shared queue and SHALL be processed one at a time in arrival order. No two AI turns SHALL run at the same time.

#### Scenario: Two messages in quick succession
- **WHEN** the owner sends "add Dune Part Three" and then "and what's downloading?" before the first turn finishes
- **THEN** the second message is processed only after the first turn completes, and it sees the first turn in its context

#### Scenario: Owner message and ask interleaved
- **WHEN** an `/ask` arrives while an owner message is being processed
- **THEN** the `/ask` turn starts after the owner's turn finishes

### Requirement: Queue survives restarts
Queued items SHALL be persisted before being acknowledged. Items not yet completed when alfred stops SHALL be processed after it restarts, in their original order.

#### Scenario: Restart with pending items
- **WHEN** alfred is restarted while two owner messages are waiting
- **THEN** both are processed after startup, in the order they arrived

### Requirement: Wait for an unreachable AI
When the AI endpoint is unreachable (connection error, timeout, or 5xx) before any tool has run in the turn, the item SHALL stay at the head of the queue and be retried with backoff. Backoff SHALL start at the configured initial delay and double up to the configured maximum, retrying without limit. Items behind it SHALL wait.

#### Scenario: AI server down
- **WHEN** the owner sends a message while `ai.lan` refuses connections
- **THEN** the message is retried at increasing intervals, and once `ai.lan` answers, the message is processed and the reply sent

#### Scenario: Order preserved during outage
- **WHEN** three messages arrive while the AI is unreachable
- **THEN** after it recovers they are processed in arrival order

### Requirement: No repeat after tools ran
If the AI becomes unreachable after a tool call in the turn has already run, alfred SHALL NOT retry the turn, so tool actions are never repeated. The turn SHALL be treated as failed and the next item processed.

#### Scenario: AI drops mid-turn
- **WHEN** a turn has called `mcparr_add_media` and the next model request times out
- **THEN** the turn is not retried, and the owner receives a failure notice saying the action may have partly completed

### Requirement: Non-retryable failures
A failure that is not an unreachable AI (for example a 4xx response or an unparseable model reply) SHALL end the turn without retrying. The item SHALL be marked failed with the reason and the next item processed.

#### Scenario: Model rejects the request
- **WHEN** the AI endpoint answers a turn with HTTP 400
- **THEN** the item is marked failed, the owner gets a failure notice if it came from Telegram, and the queue continues
