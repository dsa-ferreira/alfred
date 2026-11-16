# mcp-tools Specification

## Purpose

Defines how alfred makes the tools of the configured MCP servers available to the model: connecting with each server's credentials, naming tools so they never collide, hiding tools the owner excluded, and coping with servers that are down.

## Requirements

### Requirement: Configured MCP servers
alfred SHALL connect to each MCP server in the configuration over Streamable HTTP, sending a bearer token chosen for the turn's user: that user's `mcp_tokens` entry for the server when set, otherwise the server's own `token_env`. Servers not in the configuration SHALL NOT be contacted.

#### Scenario: Two servers configured
- **WHEN** mcparr and cookbook are configured with URLs and token variables
- **THEN** the model is offered tools from both, and each request to a server carries that server's token

#### Scenario: Per-user token
- **WHEN** `ana` has `mcp_tokens = { mcparr = "ALFRED_MCP_TOKEN_MCPARR_ANA" }`
- **THEN** listing and calls to mcparr on `ana`'s turns carry that variable's value, and on other users' turns carry `ALFRED_MCP_TOKEN_MCPARR`'s

### Requirement: Server-prefixed tool names
Each MCP tool SHALL be offered to the model as `<server>_<tool>`, where `<server>` is the configured server name. A tool call SHALL be routed to the server named by its prefix, using the original tool name.

#### Scenario: Prefixed name
- **WHEN** mcparr exposes `get_downloads`
- **THEN** the model sees `mcparr_get_downloads`, and calling it runs `get_downloads` on mcparr

#### Scenario: Same tool name on two servers
- **WHEN** two servers both expose `check_health`
- **THEN** the model sees two distinct tools and each call reaches the right server

### Requirement: Excluded tools
A server's configured `exclude_tools` (original, unprefixed names) SHALL be hidden from the model, and a call to an excluded tool SHALL be refused without contacting the server. An excluded name the server does not expose SHALL be logged as a warning.

#### Scenario: Hide a destructive tool
- **WHEN** cookbook is configured with `exclude_tools = ["delete_recipe"]`
- **THEN** the model is not offered `cookbook_delete_recipe`, and a call to it returns an error without reaching cookbook

#### Scenario: Typo in exclude list
- **WHEN** `exclude_tools` contains `delete_recipes` and cookbook has no such tool
- **THEN** alfred logs a warning naming the server and the unknown tool

### Requirement: Unavailable servers
alfred SHALL look up each server's tools at the start of every turn. A server that cannot be reached or rejects the token SHALL be left out of that turn, and the model SHALL be told which servers are unavailable. Other servers' tools SHALL still be offered.

#### Scenario: One server down
- **WHEN** mcparr is unreachable and cookbook is up
- **THEN** the turn offers only cookbook's tools, the model is told mcparr is unavailable, and the turn proceeds

#### Scenario: Server comes back
- **WHEN** mcparr was down for one turn and is reachable at the next
- **THEN** the next turn offers mcparr's tools again without restarting alfred

### Requirement: Local tools
alfred's own tools SHALL be offered alongside the MCP tools under the reserved prefix `alfred_` (for example `alfred_end_conversation`) and handled by alfred without any network call. No MCP server SHALL be configurable with the name `alfred`.

#### Scenario: Local tool offered
- **WHEN** an owner message turn starts
- **THEN** the model is offered `alfred_end_conversation` together with the MCP tools

### Requirement: Server instructions
At the start of every turn, alfred SHALL take the instructions each available MCP server sends when connecting and give them to the model, labelled with the server's name and its tool prefix `<server>_`. A server that is unavailable for the turn, or sends no or only blank instructions, SHALL contribute none.

#### Scenario: Instructions forwarded
- **WHEN** cookbook is available and its instructions say "call list_units and use its codes"
- **THEN** the model receives that text, labelled as cookbook's and noting that its tools are offered as `cookbook_<tool>`

#### Scenario: Server down
- **WHEN** cookbook is unreachable for the turn
- **THEN** the model receives no cookbook instructions, only the note that cookbook is unavailable

#### Scenario: No instructions
- **WHEN** mcparr sends no instructions
- **THEN** the model receives no mcparr section, and mcparr's tools are still offered

#### Scenario: Instructions change
- **WHEN** a server's instructions change and the server restarts
- **THEN** the next turn gives the model the new text without restarting alfred

### Requirement: Muting server instructions
A server configured with `use_server_instructions = false` SHALL have its instructions left out of every turn, while its tools are still offered. The setting SHALL default to `true`.

#### Scenario: Muted server
- **WHEN** mcparr is configured with `use_server_instructions = false` and sends instructions
- **THEN** the model receives no mcparr instructions, and mcparr's tools are offered as usual

#### Scenario: Default
- **WHEN** a server entry does not set `use_server_instructions`
- **THEN** that server's instructions are given to the model

### Requirement: Instructions length limit
Each server's instructions SHALL be cut to `[mcp] instructions_max_chars` characters (default 2000) before reaching the model, with a marker saying they were truncated, and alfred SHALL log a warning naming the server.

#### Scenario: Long instructions
- **WHEN** `instructions_max_chars` is 2000 and a server sends 5000 characters of instructions
- **THEN** the model receives the first 2000 characters followed by a truncation marker, and a warning naming the server is logged

#### Scenario: Short instructions
- **WHEN** a server sends 700 characters of instructions
- **THEN** the model receives them unchanged

### Requirement: Servers outside a user's allow-list are not contacted
On a user's turn, a server for which no allow-list entry of that user can match (no `*` entry and no entry starting with `<server>_`) SHALL NOT be contacted, SHALL contribute no instructions, and SHALL NOT be reported as unavailable.

#### Scenario: No cookbook access
- **WHEN** `ana`'s allow-list has no `cookbook_` entry and no `*`
- **THEN** cookbook is not contacted on her turns and her system prompt has no cookbook instructions or unavailable note

### Requirement: Instructions only with offered tools
A server's instructions SHALL be given to the model only when at least one of its tools is offered on the turn after the allow-list is applied.

#### Scenario: Allowed tool missing
- **WHEN** `ana`'s allow-list has only `mcparr_add_medai` (a typo) for mcparr
- **THEN** none of mcparr's tools are offered and mcparr's instructions are not sent

### Requirement: Servers may ask the user
alfred SHALL tell every MCP server it connects to that it can show form questions to the user (form elicitation), and SHALL NOT claim URL mode, sampling or roots. A tool call the server answers with a request for input SHALL be handled as `user-input-requests` says, never by answering the server without the user.

#### Scenario: Server needs a confirmation
- **WHEN** a server's tool asks the user to confirm before it acts
- **THEN** the call doesn't fail as unsupported; the turn waits for the user's answer

#### Scenario: Server asks for sampling
- **WHEN** a server answers a call by asking alfred to run a model request for it
- **THEN** the call ends with an error result for the model, and nothing is sent back to the server
