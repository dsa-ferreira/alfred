# service-api Specification

## Purpose

Defines the HTTP API homelab services use to reach the owner through alfred: sending a message directly (`/notify`) or asking the AI to decide whether the owner needs to know something (`/ask`).

## Requirements

### Requirement: Service authentication
`/notify` and `/ask` SHALL require a bearer token that matches one of the configured services. The matching service's name SHALL identify the sender. Missing, empty or unknown tokens SHALL be rejected with HTTP 401, and tokens SHALL be compared in constant time.

#### Scenario: Valid token
- **WHEN** a request carries the token configured for service `backup`
- **THEN** it is accepted and attributed to `backup`

#### Scenario: Unknown token
- **WHEN** a request carries a token that matches no configured service
- **THEN** alfred responds 401 and does nothing else

### Requirement: Direct notification
`POST /notify` with a JSON body containing non-empty `text` SHALL send the owner a Telegram message prefixed with the service name, for example `[backup] Nightly backup failed`. It SHALL NOT call the AI and SHALL NOT wait behind queued AI work.

#### Scenario: Notify while the AI is busy
- **WHEN** a service calls `/notify` while the AI is processing a long turn
- **THEN** the owner receives the notification without waiting for that turn to finish

#### Scenario: Empty text
- **WHEN** `/notify` is called with empty or missing `text`
- **THEN** alfred responds 422 and sends nothing

### Requirement: Notify delivery result
`/notify` SHALL respond 200 only after Telegram accepted the message. If delivery to Telegram fails, it SHALL respond 502 so the service can retry, and the notice SHALL NOT be recorded in the conversation.

#### Scenario: Telegram unreachable
- **WHEN** `/notify` is called and the Telegram API cannot be reached
- **THEN** alfred responds 502 and the conversation history is unchanged

### Requirement: Ask the AI
`POST /ask` with a JSON body containing non-empty `prompt` SHALL queue an AI turn on behalf of the service and respond 202 immediately with an identifier for the queued item. The response SHALL NOT wait for the AI.

#### Scenario: Ask accepted
- **WHEN** service `disk-check` calls `/ask` with prompt "/mnt/data is at 93%, was 80% yesterday"
- **THEN** alfred responds 202 with an item ID and the prompt is queued

### Requirement: AI decides whether to notify
For an `/ask` turn, the AI SHALL be told the owner sees its answer only if it is not exactly `NO_REPLY`. A final answer of `NO_REPLY` (ignoring surrounding whitespace) SHALL produce no Telegram message and SHALL only be logged. Any other answer SHALL be sent to the owner, prefixed with the service name.

#### Scenario: Nothing worth reporting
- **WHEN** the AI answers an `/ask` turn with `NO_REPLY`
- **THEN** the owner receives nothing and alfred logs the silent run with the service name and item ID

#### Scenario: Worth reporting
- **WHEN** the AI answers an `/ask` turn with "Disk usage jumped 13% overnight, mostly in /mnt/data/downloads"
- **THEN** the owner receives that text prefixed with `[disk-check]`

### Requirement: Health endpoint
`GET /healthz` SHALL respond 200 without authentication when the process is running. It SHALL NOT call Telegram, the AI or MCP servers.

#### Scenario: Health check
- **WHEN** `GET /healthz` is called without a token
- **THEN** alfred responds 200 without contacting any external system
