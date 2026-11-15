## MODIFIED Requirements

### Requirement: Summary of an ended conversation
When a conversation is archived, alfred SHALL ask the model, in the background, for a short title and a summary of one or two sentences, and store both with the conversation. The model SHALL receive at most the configured number of characters of the conversation's transcript.

#### Scenario: Conversation ends
- **WHEN** the owner ends a conversation about a lemon-garlic chicken recipe
- **THEN** that conversation is later stored with a title such as "Chicken thighs recipe" and a one- or two-sentence summary

#### Scenario: Closed for inactivity
- **WHEN** a conversation is closed for inactivity
- **THEN** it is titled and summarised the same way

#### Scenario: Very long conversation
- **WHEN** an ended conversation's transcript is longer than the configured summariser input limit
- **THEN** the model receives a shortened transcript marked as shortened, and the conversation is still summarised

### Requirement: Summary failures are retried in the background
If summarising or embedding an archived conversation fails, alfred SHALL retry in the background with backoff until it succeeds, without sending the owner any message. Background retries SHALL NOT delay owner messages or `/ask` prompts in the queue.

#### Scenario: Model fails at reset
- **WHEN** the summary request fails for a conversation that was just ended
- **THEN** the conversation is summarised later without a message to the owner, and owner messages keep being answered meanwhile

#### Scenario: Conversations from before the upgrade
- **WHEN** alfred starts with archived conversations that have no summary
- **THEN** it summarises them in the background

## ADDED Requirements

### Requirement: Summaries wait for a quiet queue
alfred SHALL start a summary request only when no queue item is waiting or running and none has finished within the configured quiet period. Embedding requests SHALL NOT wait for a quiet queue.

#### Scenario: Chatting right after a reset
- **WHEN** the user ends #41 and keeps sending messages every minute, with a 120-second quiet period
- **THEN** #41's summary request starts only once 120 seconds pass with no queue item waiting, running or finishing

#### Scenario: Ask prompts count
- **WHEN** an `/ask` prompt is waiting in the queue
- **THEN** no summary request starts

### Requirement: Queue items preempt a summary
When a queue item arrives while a summary request is running, alfred SHALL cancel that request. The cancellation SHALL NOT count as a failed attempt and SHALL NOT delay the next try beyond waiting for a quiet queue again. A title and summary already stored, and an embedding request in progress, SHALL NOT be cancelled.

#### Scenario: Message during a summary
- **WHEN** #41's summary request is running and the user sends a message
- **THEN** the summary request is cancelled, the model writes no more of that summary, and #41 is summarised after the queue is quiet again

#### Scenario: No backoff after a cancellation
- **WHEN** #41's summary request was cancelled three times by messages
- **THEN** #41 has no failed attempts recorded, and its next try starts as soon as the queue is quiet

#### Scenario: Embedding not cancelled
- **WHEN** #41's title and summary are stored, its embedding request is running, and a message arrives
- **THEN** the embedding request completes and the title, summary and embedding are kept
