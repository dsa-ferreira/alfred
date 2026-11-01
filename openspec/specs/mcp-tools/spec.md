# mcp-tools Specification

## Purpose

Defines how alfred makes the tools of the configured MCP servers available to the model: connecting with each server's credentials, naming tools so they never collide, hiding tools the owner excluded, and coping with servers that are down.

## Requirements

### Requirement: Configured MCP servers
alfred SHALL connect to each MCP server in the configuration over Streamable HTTP, sending that server's token as a bearer token. Servers not in the configuration SHALL NOT be contacted.

#### Scenario: Two servers configured
- **WHEN** mcparr and cookbook are configured with URLs and token variables
- **THEN** the model is offered tools from both, and each request to a server carries that server's token

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
