# Spec Delta

## ADDED Requirements

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
