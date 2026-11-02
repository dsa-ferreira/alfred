# Spec Delta

## ADDED Requirements

### Requirement: Typing indicator while an owner turn runs
While the AI turn for an owner message is running, alfred SHALL show the typing indicator in the owner's chat, renewing it so it stays visible for the whole turn, and SHALL stop renewing it when the turn ends. It SHALL NOT show the indicator for `/ask` turns, or while no turn is running (an item waiting in the queue or for a retry, or a summary being made after a reset).

#### Scenario: Owner turn running
- **WHEN** the owner sends "what's downloading?" and its AI turn runs for 20 seconds
- **THEN** the owner's chat shows "typing…" for those 20 seconds, until the reply arrives

#### Scenario: Turn ends
- **WHEN** an owner turn ends with a reply or a failure notice
- **THEN** alfred stops sending the typing indicator

#### Scenario: Waiting for a retry
- **WHEN** an owner message is waiting to be retried because the AI is unreachable
- **THEN** no typing indicator is sent during the wait, and it is shown again when the retried turn starts

#### Scenario: Waiting in the queue
- **WHEN** an owner message waits behind another item in the queue
- **THEN** no typing indicator is sent for it until its own turn starts

#### Scenario: Service ask
- **WHEN** an `/ask` turn runs
- **THEN** no typing indicator is sent to the owner's chat

### Requirement: Typing indicator failures don't affect the turn
If Telegram does not accept the typing indicator, alfred SHALL log it and keep trying for the rest of the turn. The failure SHALL NOT fail, delay or retry the turn, and SHALL NOT change its reply.

#### Scenario: Telegram rejects the typing action
- **WHEN** sending the typing indicator fails during an owner turn
- **THEN** the turn completes normally, the owner receives the reply, and the failure is logged

#### Scenario: Telegram recovers mid-turn
- **WHEN** one typing indicator request fails and later requests in the same turn succeed
- **THEN** the typing indicator is shown again for the rest of the turn
