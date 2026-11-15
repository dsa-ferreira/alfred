# users Specification

## Purpose

Defines who may talk to alfred and what each person may do: the configured
users, the single admin, each user's form of address and their per-tool
allow-list.

## Requirements

### Requirement: Configured users
alfred SHALL serve the users configured as `[[users]]` entries, each with a unique name and a unique Telegram user ID. Every turn SHALL belong to exactly one user: the sender of a Telegram message, the target user of a `/notify` or `/ask` call, or the recipient of a reminder.

#### Scenario: Two users
- **WHEN** `[[users]]` lists `diogo` (Telegram ID 111) and `ana` (Telegram ID 222)
- **THEN** a message from 222 starts a turn that belongs to `ana`

### Requirement: One admin
Exactly one configured user SHALL be the admin. The admin SHALL be the default target of `/notify` and `/ask` calls that name no user, and the only user who can switch brains.

#### Scenario: Notify without a user
- **WHEN** `diogo` is the admin and a service calls `/notify` without `user`
- **THEN** the notice goes to `diogo`

### Requirement: Per-tool allow-list
Each user SHALL have a tool allow-list of model-facing tool names. An entry SHALL be an exact name, a prefix pattern ending in `*` (such as `alfred_*` or `cookbook_*`), or `*` for every tool. A tool SHALL be offered on a user's turn only if the turn's own rules offer it and an allow-list entry matches it. A user with an empty allow-list SHALL be offered no tools.

#### Scenario: Partial MCP access
- **WHEN** `ana`'s allow-list is `["alfred_*", "cookbook_*", "mcparr_find_media", "mcparr_add_media"]`
- **THEN** her turns offer cookbook's tools, `mcparr_find_media` and `mcparr_add_media`, and no other mcparr tool

#### Scenario: Everything
- **WHEN** `diogo`'s allow-list is `["*"]`
- **THEN** his turns offer every tool the turn's rules offer

#### Scenario: Turn rules still apply
- **WHEN** `ana`'s allow-list is `["*"]` and an `/ask` turn for `ana` runs
- **THEN** `alfred_end_conversation` is not offered, because ask turns never offer it

### Requirement: Disallowed tool calls are refused
A call to a tool not offered to the turn's user SHALL return an "unknown tool" error result, and SHALL NOT run the tool or contact any MCP server.

#### Scenario: Model guesses a hidden tool
- **WHEN** `ana`'s allow-list does not include `mcparr_search_missing` and the model calls it on her turn
- **THEN** the result is an "unknown tool" error and mcparr is not contacted

### Requirement: Allow-list prefixes are checked
An allow-list entry SHALL start with `alfred_` or with a configured MCP server's name followed by `_`, or be `*`. Any other entry SHALL be a startup error naming the user and the entry. An exact entry naming an alfred tool that does not exist SHALL be a startup error. An exact MCP entry the server does not expose SHALL be logged as a warning when the server's tools are listed.

#### Scenario: Unknown server prefix
- **WHEN** an allow-list contains `sonarr_add_series` and no server is named `sonarr`
- **THEN** alfred exits at startup with an error naming the user and `sonarr_add_series`

#### Scenario: Typo in an MCP tool name
- **WHEN** an allow-list contains `mcparr_add_medai`
- **THEN** alfred starts and logs a warning naming the user, mcparr and `mcparr_add_medai` when mcparr's tools are listed

### Requirement: Users don't see each other's data
A user's turns and commands SHALL NOT see another user's conversations, history, archived conversations, attachments, facts or reminders, except reminders on which that user is the creator or a recipient.

#### Scenario: Searching memory
- **WHEN** `ana` asks "what did we talk about yesterday?"
- **THEN** only `ana`'s archived conversations can be returned

#### Scenario: Facts
- **WHEN** `diogo` has facts and `ana`'s turn runs, or `ana` sends `/memory`
- **THEN** none of `diogo`'s facts are in `ana`'s prompt, tool results or `/memory` replies

### Requirement: The model knows who it is talking to
The system prompt of every turn SHALL name the user whose turn it is. When that user is offered `alfred_remind_set`, it SHALL also name the other users that can be reminded.

#### Scenario: Reminder for someone else
- **WHEN** `diogo` is offered `alfred_remind_set` and `ana` is the other user
- **THEN** his system prompt says he is `diogo` and that `ana` can also be reminded
