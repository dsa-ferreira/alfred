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
`POST /notify` with a JSON body containing non-empty `text` and an optional `user` SHALL send the target user a Telegram message prefixed with the service name, for example `[backup] Nightly backup failed`. The target SHALL be the configured user named by `user`, or the admin when `user` is absent. It SHALL NOT call the AI and SHALL NOT wait behind queued AI work.

#### Scenario: Notify while the AI is busy
- **WHEN** a service calls `/notify` while the AI is processing a long turn
- **THEN** the target user receives the notification without waiting for that turn to finish

#### Scenario: Notify a named user
- **WHEN** `mcparr` calls `/notify` with `user` `ana` and text "Dune finished downloading"
- **THEN** `ana` receives `[mcparr] Dune finished downloading` and the admin receives nothing

#### Scenario: No user given
- **WHEN** `backup` calls `/notify` without `user`
- **THEN** the admin receives the notification

#### Scenario: Empty text
- **WHEN** `/notify` is called with empty or missing `text`
- **THEN** alfred responds 422 and sends nothing

### Requirement: Notify delivery result
`/notify` SHALL respond 200 only after Telegram accepted the message. If delivery to Telegram fails, it SHALL respond 502 so the service can retry, and the notice SHALL NOT be recorded in the conversation.

#### Scenario: Telegram unreachable
- **WHEN** `/notify` is called and the Telegram API cannot be reached
- **THEN** alfred responds 502 and the conversation history is unchanged

### Requirement: Ask the AI
`POST /ask` with a JSON body containing non-empty `prompt` and an optional `user` SHALL queue an AI turn on behalf of the service for the target user (the user named by `user`, or the admin when it is absent) and respond 202 immediately with an identifier for the queued item. The response SHALL NOT wait for the AI. The turn SHALL use the target user's conversation, form of address and tool allow-list.

#### Scenario: Ask accepted
- **WHEN** service `disk-check` calls `/ask` with prompt "/mnt/data is at 93%, was 80% yesterday"
- **THEN** alfred responds 202 with an item ID and the prompt is queued for the admin

#### Scenario: Ask for a named user
- **WHEN** a service calls `/ask` with `user` `ana`
- **THEN** the turn runs with `ana`'s history and only the tools her allow-list matches

### Requirement: AI decides whether to notify
For an `/ask` turn, the AI SHALL be told its target user sees its answer only if it is not exactly `NO_REPLY`. A final answer of `NO_REPLY` (ignoring surrounding whitespace) SHALL produce no Telegram message and SHALL only be logged. Any other answer SHALL be sent to the target user, prefixed with the service name.

#### Scenario: Nothing worth reporting
- **WHEN** the AI answers an `/ask` turn with `NO_REPLY`
- **THEN** nobody receives anything and alfred logs the silent run with the service name, item ID and target user

#### Scenario: Worth reporting
- **WHEN** the AI answers an `/ask` turn for the admin with "Disk usage jumped 13% overnight, mostly in /mnt/data/downloads"
- **THEN** the admin receives that text prefixed with `[disk-check]`

### Requirement: Health endpoint
`GET /healthz` SHALL respond 200 without authentication when the process is running. It SHALL NOT call Telegram, the AI or MCP servers.

#### Scenario: Health check
- **WHEN** `GET /healthz` is called without a token
- **THEN** alfred responds 200 without contacting any external system

### Requirement: Unknown target user
A `/notify` or `/ask` call whose `user` is not a configured user's name SHALL be rejected with HTTP 422 naming the problem, and SHALL send and queue nothing.

#### Scenario: Misspelt user
- **WHEN** a service calls `/notify` with `user` `anna` and no user is named `anna`
- **THEN** alfred responds 422 and nobody receives anything
